import difflib
import json
import shlex
from collections import defaultdict, deque
from dataclasses import dataclass


IDENTITY_ATTRIBUTE_NAMES = {
    "name",
    "bucket",
    "filename",
    "domain",
    "zone_id",
    "arn",
    "repository",
    "content_sha256",
    "secret_id",
    "member",
    "role",
    "project",
}

NOISY_ATTRIBUTE_NAMES = {
    "id",
    "etag",
    "self_link",
    "last_modified",
    "created_at",
    "updated_at",
    "create_time",
    "update_time",
    "version",
}

LOW_VALUE_ATTRIBUTE_NAMES = {
    "acl",
    "enabled",
    "file_permission",
    "directory_permission",
    "mode",
}

MINIMUM_SCORE = 0.78
AMBIGUITY_MARGIN = 0.08
MINIMUM_WEIGHTED_EVIDENCE = 2.0


@dataclass(frozen=True)
class PreparedResource:
    address: str
    resource_type: str
    flat_state: dict
    identity_values: tuple
    fingerprint: tuple
    fingerprint_weight: float


@dataclass(frozen=True)
class CandidateMatch:
    destroyed: PreparedResource
    created: PreparedResource
    state_score: float
    address_score: float
    final_score: float
    comparable_weight: float


@dataclass(frozen=True)
class AssignmentResult:
    matches: tuple
    total_score: float


def load_plan(plan_path):
    with open(plan_path, 'r') as f:
        plan = json.load(f)
    return plan


def flatten_dict(d, parent_key='', sep='.'):
    items = []
    if not isinstance(d, dict):
        return items

    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep))
        elif isinstance(v, list):
            for index, item in enumerate(v):
                item_key = f"{new_key}{sep}{index}"
                if isinstance(item, dict):
                    items.extend(flatten_dict(item, item_key, sep=sep))
                else:
                    items.append((item_key, str(item)))
        else:
            items.append((new_key, str(v)))
    return items


def get_resource_state(resource, state_key):
    return set(get_flat_resource_state(resource, state_key).items())


def get_flat_resource_state(resource, state_key):
    state = resource['change'].get(state_key) or {}
    return dict(flatten_dict(state))


def get_resource_changes(plan):
    return plan.get('resource_changes', [])


def filter_resources_by_action(resource_changes, action):
    return [
        res for res in resource_changes
        if res.get('change', {}).get('actions', []) == [action]
    ]


def attribute_name(attribute_path):
    parts = [
        part for part in attribute_path.lower().split(".")
        if not part.isdigit()
    ]
    return parts[-1] if parts else attribute_path.lower()


def is_unknown_value(value):
    return value in {"", "None", "<unknown>", "(known after apply)"}


def is_noisy_attribute(attribute_path, value):
    name = attribute_name(attribute_path)

    if name in IDENTITY_ATTRIBUTE_NAMES:
        return False

    lowered_path = attribute_path.lower()
    return (
        name in NOISY_ATTRIBUTE_NAMES
        or lowered_path.startswith("timeouts.")
        or is_unknown_value(value)
    )


def attribute_weight(attribute_path, value):
    name = attribute_name(attribute_path)

    if name in IDENTITY_ATTRIBUTE_NAMES:
        return 5.0
    if name in LOW_VALUE_ATTRIBUTE_NAMES:
        return 0.5
    if value in {"True", "False"}:
        return 0.25
    if "permission" in name:
        return 0.5
    if attribute_path.lower().startswith(("labels.", "tags.")):
        return 1.0
    if name == "content":
        return 2.0

    return 1.0


def stable_state_items(flat_state):
    return {
        key: value
        for key, value in flat_state.items()
        if not is_noisy_attribute(key, value)
    }


def extract_identity_values(flat_state):
    values = []

    for key, value in sorted(stable_state_items(flat_state).items()):
        name = attribute_name(key)
        if name in IDENTITY_ATTRIBUTE_NAMES:
            values.append(f"{name}={value}")

    return tuple(values)


def build_fingerprint(flat_state):
    stable_items = stable_state_items(flat_state)
    return tuple(sorted(stable_items.items()))


def fingerprint_weight(fingerprint):
    return sum(
        attribute_weight(attribute_path, value)
        for attribute_path, value in fingerprint
    )


def prepare_resource(resource, state_key):
    flat_state = get_flat_resource_state(resource, state_key)
    fingerprint = build_fingerprint(flat_state)

    return PreparedResource(
        address=resource["address"],
        resource_type=resource["type"],
        flat_state=flat_state,
        identity_values=extract_identity_values(flat_state),
        fingerprint=fingerprint,
        fingerprint_weight=fingerprint_weight(fingerprint),
    )


def prepare_resources(resources, state_key):
    return [
        prepare_resource(resource, state_key)
        for resource in resources
    ]


def compute_weighted_state_score(destroyed, created):
    matched_weight = 0.0
    comparable_weight = 0.0

    attribute_paths = sorted(
        set(destroyed.flat_state) | set(created.flat_state)
    )

    for attribute_path in attribute_paths:
        destroyed_value = destroyed.flat_state.get(attribute_path)
        created_value = created.flat_state.get(attribute_path)
        reference_value = (
            destroyed_value
            if destroyed_value is not None
            else created_value
        )

        if is_noisy_attribute(attribute_path, reference_value):
            continue

        weight = attribute_weight(attribute_path, reference_value)
        comparable_weight += weight

        if (
            destroyed_value is not None
            and created_value is not None
            and destroyed_value == created_value
        ):
            matched_weight += weight

    if comparable_weight == 0:
        return 0.0, 0.0

    return matched_weight / comparable_weight, comparable_weight


def calculate_match_scores(destroyed_resources, created_resources):
    match_scores = {}
    prepared_destroyed = prepare_resources(destroyed_resources, 'before')
    prepared_created = prepare_resources(created_resources, 'after')

    for res_destroy in prepared_destroyed:
        res_destroy_address = res_destroy.address
        match_scores.setdefault(res_destroy_address, {})

        for res_create in prepared_created:
            res_create_address = res_create.address
            match_scores[res_destroy_address].setdefault(res_create_address, {})
            state_score, comparable_weight = compute_weighted_state_score(
                res_destroy,
                res_create,
            )
            address_score = compute_address_similarity(
                res_destroy_address,
                res_create_address,
            )
            scores = match_scores[res_destroy_address][res_create_address]
            scores["state_match"] = state_score
            scores["state_score"] = state_score
            scores["address_score"] = address_score
            scores["comparable_weight"] = comparable_weight
            scores["aggregated"] = aggregate_scores(scores)

    return match_scores


def compute_address_similarity(address_destroy, address_create):
    return difflib.SequenceMatcher(None, address_destroy, address_create).ratio()


def compute_similarity_scores(address_destroy, address_create):
    return {
        "address": compute_address_similarity(address_destroy, address_create),
    }


def aggregate_scores(scores):
    state_score = scores.get("state_score", scores.get("state_match", 0.0))
    address_score = scores.get("address_score", scores.get("address", 0.0))
    return (0.85 * state_score) + (0.15 * address_score)


def build_state_mv_command(source_address, destination_address):
    return (
        "terraform state mv "
        f"{shlex.quote(source_address)} {shlex.quote(destination_address)}"
    )


def validate_resource_type_counts(destroyed_by_type, created_by_type):
    resource_types = sorted(set(destroyed_by_type) | set(created_by_type))
    mismatches = []

    for res_type in resource_types:
        destroyed_count = len(destroyed_by_type.get(res_type, []))
        created_count = len(created_by_type.get(res_type, []))

        if destroyed_count != created_count:
            mismatches.append((res_type, destroyed_count, created_count))

    if not mismatches:
        return

    for res_type, destroyed_count, created_count in mismatches:
        print(f"Error: Mismatch for resource type '{res_type}'")
        print(f"  Destroyed: {destroyed_count} resource(s)")
        print(f"  Created: {created_count} resource(s)")
    print("Cannot proceed with matching because the numbers don't match.")
    raise SystemExit(1)


def split_address(address):
    parts = []
    current = []
    bracket_depth = 0
    quote_char = None
    escaped = False

    for char in address:
        if escaped:
            current.append(char)
            escaped = False
            continue

        if quote_char:
            current.append(char)
            if char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue

        if char in {"'", '"'}:
            current.append(char)
            quote_char = char
        elif char == "[":
            current.append(char)
            bracket_depth += 1
        elif char == "]":
            current.append(char)
            bracket_depth = max(0, bracket_depth - 1)
        elif char == "." and bracket_depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)

    parts.append("".join(current))
    return parts


def common_suffix_length(source_parts, destination_parts):
    length = 0
    for source_part, destination_part in zip(
        reversed(source_parts),
        reversed(destination_parts),
    ):
        if source_part != destination_part:
            break
        length += 1

    return length


def derive_address_contexts(accepted_matches):
    source_to_destinations = defaultdict(set)
    destination_to_sources = defaultdict(set)

    for source_address, destination_address, _score, _reason in accepted_matches:
        source_parts = split_address(source_address)
        destination_parts = split_address(destination_address)
        suffix_length = common_suffix_length(source_parts, destination_parts)

        if (
            suffix_length < 2
            or suffix_length >= len(source_parts)
            or suffix_length >= len(destination_parts)
        ):
            continue

        source_prefix = ".".join(source_parts[:-suffix_length])
        destination_prefix = ".".join(destination_parts[:-suffix_length])

        if not source_prefix or not destination_prefix:
            continue
        if source_prefix == destination_prefix:
            continue

        source_to_destinations[source_prefix].add(destination_prefix)
        destination_to_sources[destination_prefix].add(source_prefix)

    contexts = {}
    for source_prefix, destination_prefixes in source_to_destinations.items():
        if len(destination_prefixes) != 1:
            continue

        destination_prefix = next(iter(destination_prefixes))
        if len(destination_to_sources[destination_prefix]) != 1:
            continue

        contexts[source_prefix] = destination_prefix

    return contexts


def apply_address_context(address, source_prefix, destination_prefix):
    if address == source_prefix:
        return destination_prefix
    if address.startswith(f"{source_prefix}."):
        return f"{destination_prefix}{address[len(source_prefix):]}"

    return None


def is_context_candidate_compatible(candidate):
    return (
        candidate.comparable_weight >= MINIMUM_WEIGHTED_EVIDENCE
        and candidate.state_score >= 0.5
    )


def find_address_context_matches(
    unmatched_destroyed_by_type,
    unmatched_created_by_type,
    accepted_matches,
):
    contexts = derive_address_contexts(accepted_matches)
    matches = []
    used_destroyed = set()
    used_created = set()

    for resource_type in sorted(unmatched_destroyed_by_type):
        unmatched_destroyed = unmatched_destroyed_by_type[resource_type]
        unmatched_created = unmatched_created_by_type.get(resource_type, {})

        for destroyed_address in sorted(unmatched_destroyed):
            if destroyed_address in used_destroyed:
                continue

            destroyed = unmatched_destroyed[destroyed_address]
            context_candidates = []

            for source_prefix, destination_prefix in sorted(
                contexts.items(),
                key=lambda item: (-len(item[0]), item[0]),
            ):
                candidate_address = apply_address_context(
                    destroyed_address,
                    source_prefix,
                    destination_prefix,
                )

                if candidate_address is None:
                    continue
                if candidate_address not in unmatched_created:
                    continue
                if candidate_address in used_created:
                    continue

                candidate = build_candidate(
                    destroyed,
                    unmatched_created[candidate_address],
                )
                if is_context_candidate_compatible(candidate):
                    context_candidates.append(candidate)

            if len(context_candidates) != 1:
                continue

            candidate = context_candidates[0]
            matches.append((
                candidate.destroyed,
                candidate.created,
                candidate.final_score,
                "address context",
            ))
            used_destroyed.add(candidate.destroyed.address)
            used_created.add(candidate.created.address)

    return matches


def accept_context_matches(
    matches,
    unmatched_destroyed_by_type,
    unmatched_created_by_type,
    accepted_matches,
):
    accepted = 0

    for destroyed, created, score, reason in matches:
        unmatched_destroyed = unmatched_destroyed_by_type[destroyed.resource_type]
        unmatched_created = unmatched_created_by_type[created.resource_type]

        if (
            destroyed.address not in unmatched_destroyed
            or created.address not in unmatched_created
        ):
            continue

        accepted_matches.append((
            destroyed.address,
            created.address,
            score,
            reason,
        ))
        del unmatched_destroyed[destroyed.address]
        del unmatched_created[created.address]
        accepted += 1

    return accepted


def build_candidate(destroyed, created):
    state_score, comparable_weight = compute_weighted_state_score(
        destroyed,
        created,
    )
    address_score = compute_address_similarity(
        destroyed.address,
        created.address,
    )
    final_score = aggregate_scores({
        "state_score": state_score,
        "address_score": address_score,
    })

    return CandidateMatch(
        destroyed=destroyed,
        created=created,
        state_score=state_score,
        address_score=address_score,
        final_score=final_score,
        comparable_weight=comparable_weight,
    )


def candidate_sort_key(candidate):
    return (
        -candidate.final_score,
        -candidate.state_score,
        candidate.created.address,
    )


def solve_global_assignment(candidates_by_destroyed, forbidden_pairs=None):
    forbidden_pairs = forbidden_pairs or set()
    destroyed_addresses = sorted(candidates_by_destroyed)
    created_addresses = sorted({
        candidate.created.address
        for candidates in candidates_by_destroyed.values()
        for candidate in candidates
    })
    target_flow = len(destroyed_addresses)

    if target_flow == 0:
        return AssignmentResult((), 0.0)
    if len(created_addresses) != target_flow:
        return None

    source = 0
    destroyed_offset = 1
    created_offset = destroyed_offset + len(destroyed_addresses)
    sink = created_offset + len(created_addresses)
    graph = [[] for _ in range(sink + 1)]
    score_scale = 1_000_000

    def add_edge(from_node, to_node, capacity, cost, candidate=None):
        forward = {
            "to": to_node,
            "rev": len(graph[to_node]),
            "capacity": capacity,
            "cost": cost,
            "candidate": candidate,
        }
        reverse = {
            "to": from_node,
            "rev": len(graph[from_node]),
            "capacity": 0,
            "cost": -cost,
            "candidate": None,
        }
        graph[from_node].append(forward)
        graph[to_node].append(reverse)

    for index, destroyed_address in enumerate(destroyed_addresses):
        add_edge(source, destroyed_offset + index, 1, 0)

    for index, created_address in enumerate(created_addresses):
        add_edge(created_offset + index, sink, 1, 0)

    created_index_by_address = {
        address: index for index, address in enumerate(created_addresses)
    }
    for destroyed_index, destroyed_address in enumerate(destroyed_addresses):
        from_node = destroyed_offset + destroyed_index
        for candidate in candidates_by_destroyed[destroyed_address]:
            pair = (candidate.destroyed.address, candidate.created.address)
            if pair in forbidden_pairs:
                continue

            to_node = (
                created_offset
                + created_index_by_address[candidate.created.address]
            )
            add_edge(
                from_node,
                to_node,
                1,
                round(candidate.final_score * score_scale),
                candidate,
            )

    total_cost = 0
    flow = 0

    while flow < target_flow:
        distances = [float("-inf")] * len(graph)
        previous = [None] * len(graph)
        in_queue = [False] * len(graph)
        queue = deque([source])
        distances[source] = 0
        in_queue[source] = True

        while queue:
            node = queue.popleft()
            in_queue[node] = False

            for edge_index, edge in enumerate(graph[node]):
                if edge["capacity"] <= 0:
                    continue

                next_node = edge["to"]
                next_distance = distances[node] + edge["cost"]
                if next_distance <= distances[next_node]:
                    continue

                distances[next_node] = next_distance
                previous[next_node] = (node, edge_index)
                if not in_queue[next_node]:
                    queue.append(next_node)
                    in_queue[next_node] = True

        if previous[sink] is None:
            return None

        node = sink
        while node != source:
            previous_node, edge_index = previous[node]
            edge = graph[previous_node][edge_index]
            edge["capacity"] -= 1
            graph[node][edge["rev"]]["capacity"] += 1
            total_cost += edge["cost"]
            node = previous_node

        flow += 1

    matches = []
    for destroyed_index, _destroyed_address in enumerate(destroyed_addresses):
        node = destroyed_offset + destroyed_index
        for edge in graph[node]:
            if edge["candidate"] is not None and edge["capacity"] == 0:
                matches.append(edge["candidate"])
                break

    return AssignmentResult(
        tuple(sorted(matches, key=lambda candidate: candidate.destroyed.address)),
        total_cost / score_scale,
    )


def is_globally_confident_assignment(candidate, assignment, candidates_by_destroyed):
    if candidate.final_score < MINIMUM_SCORE:
        return False
    if candidate.comparable_weight < MINIMUM_WEIGHTED_EVIDENCE:
        return False
    if len(assignment.matches) == 1:
        return True

    alternative = solve_global_assignment(
        candidates_by_destroyed,
        forbidden_pairs={
            (
                candidate.destroyed.address,
                candidate.created.address,
            )
        },
    )
    if alternative is None:
        return True

    return assignment.total_score - alternative.total_score >= AMBIGUITY_MARGIN


def find_exact_fingerprint_matches(unmatched_destroyed, unmatched_created):
    destroyed_by_fingerprint = defaultdict(list)
    created_by_fingerprint = defaultdict(list)

    for resource in unmatched_destroyed.values():
        if (
            resource.fingerprint
            and resource.fingerprint_weight >= MINIMUM_WEIGHTED_EVIDENCE
        ):
            destroyed_by_fingerprint[resource.fingerprint].append(resource)
    for resource in unmatched_created.values():
        if (
            resource.fingerprint
            and resource.fingerprint_weight >= MINIMUM_WEIGHTED_EVIDENCE
        ):
            created_by_fingerprint[resource.fingerprint].append(resource)

    matches = []
    shared_fingerprints = (
        set(destroyed_by_fingerprint) & set(created_by_fingerprint)
    )
    for fingerprint in sorted(shared_fingerprints):
        destroyed_resources = destroyed_by_fingerprint[fingerprint]
        created_resources = created_by_fingerprint[fingerprint]

        if len(destroyed_resources) == 1 and len(created_resources) == 1:
            destroyed = destroyed_resources[0]
            created = created_resources[0]
            if (
                destroyed.address in unmatched_destroyed
                and created.address in unmatched_created
            ):
                matches.append((destroyed, created, 1.0, "exact fingerprint"))

    return matches


def find_unique_identity_matches(unmatched_destroyed, unmatched_created):
    destroyed_by_identity = defaultdict(list)
    created_by_identity = defaultdict(list)

    for resource in unmatched_destroyed.values():
        for identity_value in resource.identity_values:
            destroyed_by_identity[identity_value].append(resource)
    for resource in unmatched_created.values():
        for identity_value in resource.identity_values:
            created_by_identity[identity_value].append(resource)

    identity_pairs = defaultdict(list)
    shared_identity_values = (
        set(destroyed_by_identity) & set(created_by_identity)
    )
    for identity_value in sorted(shared_identity_values):
        destroyed_resources = destroyed_by_identity[identity_value]
        created_resources = created_by_identity[identity_value]

        if len(destroyed_resources) == 1 and len(created_resources) == 1:
            identity_pairs[
                (
                    destroyed_resources[0].address,
                    created_resources[0].address,
                )
            ].append(identity_value)

    destroyed_destinations = defaultdict(set)
    created_sources = defaultdict(set)
    for destroyed_address, created_address in identity_pairs:
        destroyed_destinations[destroyed_address].add(created_address)
        created_sources[created_address].add(destroyed_address)

    matches = []
    for destroyed_address, created_address in sorted(identity_pairs):
        if (
            len(destroyed_destinations[destroyed_address]) == 1
            and len(created_sources[created_address]) == 1
            and destroyed_address in unmatched_destroyed
            and created_address in unmatched_created
        ):
            matches.append((
                unmatched_destroyed[destroyed_address],
                unmatched_created[created_address],
                1.0,
                "unique identity",
            ))

    return matches


def find_scored_matches(unmatched_destroyed, unmatched_created):
    if not unmatched_destroyed or not unmatched_created:
        return []

    candidates_by_destroyed = {}

    for destroyed in unmatched_destroyed.values():
        candidates = sorted(
            [
                build_candidate(destroyed, created)
                for created in unmatched_created.values()
            ],
            key=candidate_sort_key,
        )
        candidates_by_destroyed[destroyed.address] = candidates

    assignment = solve_global_assignment(candidates_by_destroyed)
    if assignment is None:
        return []

    matches = []
    for candidate in assignment.matches:
        if not is_globally_confident_assignment(
            candidate,
            assignment,
            candidates_by_destroyed,
        ):
            continue

        matches.append((
            candidate.destroyed,
            candidate.created,
            candidate.final_score,
            "weighted state",
        ))

    return matches


def build_ambiguity_reports(unmatched_destroyed, unmatched_created):
    reports = []

    for destroyed in sorted(
        unmatched_destroyed.values(),
        key=lambda item: item.address,
    ):
        candidates = sorted(
            [
                build_candidate(destroyed, created)
                for created in unmatched_created.values()
            ],
            key=candidate_sort_key,
        )
        candidates = [
            candidate for candidate in candidates
            if candidate.comparable_weight >= MINIMUM_WEIGHTED_EVIDENCE
        ]

        if len(candidates) < 2:
            continue

        best = candidates[0]
        second = candidates[1]
        if (
            best.final_score >= MINIMUM_SCORE
            and best.final_score - second.final_score < AMBIGUITY_MARGIN
        ):
            reports.append((destroyed, candidates[:3]))

    return reports


def accept_matches(matches, unmatched_destroyed, unmatched_created, accepted_matches):
    accepted = 0

    for destroyed, created, score, reason in matches:
        if (
            destroyed.address not in unmatched_destroyed
            or created.address not in unmatched_created
        ):
            continue

        accepted_matches.append((
            destroyed.address,
            created.address,
            score,
            reason,
        ))
        del unmatched_destroyed[destroyed.address]
        del unmatched_created[created.address]
        accepted += 1

    return accepted


def match_resource_group(destroyed_resources, created_resources):
    destroyed = prepare_resources(destroyed_resources, "before")
    created = prepare_resources(created_resources, "after")
    unmatched_destroyed = {
        resource.address: resource for resource in destroyed
    }
    unmatched_created = {
        resource.address: resource for resource in created
    }
    accepted_matches = []

    accept_matches(
        find_exact_fingerprint_matches(unmatched_destroyed, unmatched_created),
        unmatched_destroyed,
        unmatched_created,
        accepted_matches,
    )

    while accept_matches(
        find_unique_identity_matches(unmatched_destroyed, unmatched_created),
        unmatched_destroyed,
        unmatched_created,
        accepted_matches,
    ):
        pass

    while accept_matches(
        find_scored_matches(unmatched_destroyed, unmatched_created),
        unmatched_destroyed,
        unmatched_created,
        accepted_matches,
    ):
        pass

    return accepted_matches, unmatched_destroyed, unmatched_created


def print_ambiguity_reports(ambiguity_reports):
    if not ambiguity_reports:
        return

    print("Ambiguous Matches:")
    for destroyed, candidates in ambiguity_reports:
        print("Ambiguous match:")
        print(f"  destroyed: {destroyed.address}")
        print("  candidates:")
        for candidate in candidates:
            print(
                f"    - {candidate.created.address} "
                f"score={candidate.final_score:.2f}"
            )


def print_matches(matches):
    if not matches:
        return

    print("Matched Resources:")
    for source_address, destination_address, score, reason in sorted(matches):
        print(f"Matched Destroyed Resource: {source_address}")
        print(f"With Created Resource: {destination_address}")
        print(f"Total Similarity Score: {score:.2f}")
        print(f"Match Reason: {reason}")
    print()


def main(plan_path, output_path):
    plan = load_plan(plan_path)
    resource_changes = get_resource_changes(plan)
    destroyed_resources = filter_resources_by_action(resource_changes, 'delete')
    created_resources = filter_resources_by_action(resource_changes, 'create')

    # Group resources by type for efficiency
    destroyed_by_type = defaultdict(list)
    for res in destroyed_resources:
        destroyed_by_type[res['type']].append(res)
    created_by_type = defaultdict(list)
    for res in created_resources:
        created_by_type[res['type']].append(res)

    validate_resource_type_counts(destroyed_by_type, created_by_type)

    move_commands = []
    best_matches = []
    unmatched_destroyed_by_type = {}
    unmatched_created_by_type = {}

    for res_type in sorted(destroyed_by_type.keys()):
        (
            type_matches,
            type_unmatched_destroyed,
            type_unmatched_created,
        ) = match_resource_group(
            destroyed_by_type[res_type],
            created_by_type.get(res_type, []),
        )
        best_matches.extend(type_matches)
        unmatched_destroyed_by_type[res_type] = type_unmatched_destroyed
        unmatched_created_by_type[res_type] = type_unmatched_created

    while accept_context_matches(
        find_address_context_matches(
            unmatched_destroyed_by_type,
            unmatched_created_by_type,
            best_matches,
        ),
        unmatched_destroyed_by_type,
        unmatched_created_by_type,
        best_matches,
    ):
        pass

    ambiguity_reports = []
    for res_type in sorted(unmatched_destroyed_by_type):
        ambiguity_reports.extend(build_ambiguity_reports(
            unmatched_destroyed_by_type[res_type],
            unmatched_created_by_type.get(res_type, {}),
        ))

    print_matches(best_matches)
    print_ambiguity_reports(ambiguity_reports)

    unmatched_res_destroy = {
        address
        for resources in unmatched_destroyed_by_type.values()
        for address in resources
    }
    unmatched_res_create = {
        address
        for resources in unmatched_created_by_type.values()
        for address in resources
    }

    if len(unmatched_res_create) > 0:
        print("Unmatched Created Resources:")
    for unmatched_create in sorted(unmatched_res_create):
        print(f' - {unmatched_create}')
    if len(unmatched_res_destroy) > 0:
        print("Unmatched Destroyed Resources:")
    for unmatched_destroy in sorted(unmatched_res_destroy):
        print(f' - {unmatched_destroy}')
    print()

    for match in best_matches:
        command = build_state_mv_command(match[0], match[1])
        move_commands.append(command)

    # Write the move commands to the output file
    with open(output_path, 'w') as f:
        for command in move_commands:
            f.write(command + '\n')

    print(f"Terraform move commands have been written to {output_path}")


def cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description='Terraform Resource Matcher')
    parser.add_argument('--plan', required=True, help='Path to tfplan.json')
    parser.add_argument('--output', default='terraform_move_commands.sh',
                        help='Path to output file for terraform move commands')
    args = parser.parse_args(argv)
    main(args.plan, args.output)


if __name__ == "__main__":
    cli()
