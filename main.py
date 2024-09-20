import difflib
from collections import defaultdict

from sklearn.feature_extraction.text import CountVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import json

import jellyfish


def load_plan(plan_path):
    with open(plan_path, 'r') as f:
        plan = json.load(f)
    return plan


def flatten_dict(d, parent_key='', sep='.'):
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep))
        else:
            items.append((new_key, str(v)))
    return items


def get_resource_state(resource, state_key):
    state = resource['change'].get(state_key, {})
    flat_state = flatten_dict(state)
    return set(flat_state)


def get_resource_changes(plan):
    return plan.get('resource_changes', [])


def filter_resources_by_action(resource_changes, action):
    return [
        res for res in resource_changes
        if res.get('change', {}).get('actions', []) == [action]
    ]


def calculate_match_scores(destroyed_resources, created_resources):
    match_scores = {}

    for res_destroy in destroyed_resources:
        res_destroy_address = res_destroy['address']
        state_destroy = get_resource_state(res_destroy, 'before')
        match_scores.setdefault(res_destroy_address, {})

        for res_create in created_resources:
            res_create_address = res_create['address']
            match_scores[res_destroy_address].setdefault(res_create_address, {})
            state_create = get_resource_state(res_create, 'after')
            match_count = len(state_destroy.intersection(state_create))
            match_scores[res_destroy_address][res_create_address]["state_match"] = match_count

            similarity_scores = compute_similarity_scores(res_destroy_address, res_create_address)
            for key, value in similarity_scores.items():
                match_scores[res_destroy_address][res_create_address][key] = value
            match_scores[res_destroy_address][res_create_address]["aggregated"] = aggregate_scores(
                match_scores[res_destroy_address][res_create_address])

    return match_scores


def compute_similarity_scores(address_destroy, address_create):
    scores = {}

    # Levenshtein Similarity
    distance = jellyfish.levenshtein_distance(address_destroy, address_create)
    max_len = max(len(address_destroy), len(address_create))
    scores['levenshtein'] = 1 - (distance / max_len) if max_len > 0 else 1.0

    # Jaro-Winkler Similarity
    scores['jaro_winkler'] = jellyfish.jaro_winkler_similarity(address_destroy, address_create)

    # Damerau-Levenshtein Similarity
    distance_damerau = jellyfish.damerau_levenshtein_distance(address_destroy, address_create)
    scores['damerau_levenshtein'] = 1 - (distance_damerau / max_len) if max_len > 0 else 1.0

    # Ratcliff/Obershelp Similarity
    scores['ratcliff_obershelp'] = difflib.SequenceMatcher(None, address_destroy, address_create).ratio()

    # Cosine Similarity on Character N-Grams (e.g., bigrams)
    vectorizer = CountVectorizer(analyzer='char', ngram_range=(2, 2))
    vectors = vectorizer.fit_transform([address_destroy, address_create]).toarray()
    cosine_sim = cosine_similarity(vectors)[0][1]
    scores['cosine'] = cosine_sim

    return scores


def aggregate_scores(scores):
    weights = {
        'state_match': 10,
        'levenshtein': 0.5,
        'jaro_winkler': 1,
        'damerau_levenshtein': 0.5,
        'ratcliff_obershelp': 1,
        'cosine': 1.5
    }
    total_weight = sum(weights.values())
    weighted_score = sum(scores[alg] * weights.get(alg, 1) for alg in scores)
    return weighted_score / total_weight if total_weight > 0 else 0


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

    # Initialize list to store move commands
    move_commands = []

    # Perform matching
    match_scores = {}
    for res_type in destroyed_by_type.keys():
        match_scores = match_scores | calculate_match_scores(
            destroyed_by_type[res_type],
            created_by_type.get(res_type, [])
        )

    best_matches = []

    # Set to track unmatched res_destroy and res_create
    unmatched_res_destroy = set(match_scores.keys())
    unmatched_res_create = set()

    # Collect all res_create values in a set
    for creates in match_scores.values():
        unmatched_res_create.update(creates.keys())

    while match_scores:
        best_res_destroy = None
        best_res_create = None
        highest_aggregated_score = float('-inf')

        # Find the (res_destroy, res_create) pair with the highest aggregated score
        for res_destroy, creates in match_scores.items():
            for res_create, scores in creates.items():
                aggregated_score = scores.get("aggregated", 0)

                if aggregated_score > highest_aggregated_score:
                    highest_aggregated_score = aggregated_score
                    best_res_destroy = res_destroy
                    best_res_create = res_create

        # If no valid match is found, break the loop
        if best_res_destroy is None or best_res_create is None:
            break

        # Store the best match
        best_matches.append((best_res_destroy, best_res_create, highest_aggregated_score))

        # Remove the res_create from all other res_destroy entries
        for res_destroy in list(match_scores.keys()):
            if best_res_create in match_scores[res_destroy]:
                del match_scores[res_destroy][best_res_create]

            # If no more creates remain for this res_destroy, remove the res_destroy as well
            if not match_scores[res_destroy]:
                del match_scores[res_destroy]

        # Also remove the best_res_destroy itself, as it has been matched
        if best_res_destroy in match_scores:
            del match_scores[best_res_destroy]

        unmatched_res_create.discard(best_res_create)
        unmatched_res_destroy.discard(best_res_destroy)

    if len(unmatched_res_create) > 0:
        print("Unmatched Created Resources:")
    for unmatched_create in unmatched_res_create:
        print(f' - {unmatched_create}')
    if len(unmatched_res_destroy) > 0:
        print("Unmatched Destroyed Resources:")
    for unmatched_destroy in unmatched_res_destroy:
        print(f' - {unmatched_destroy}')
    print()

    for match in best_matches:
        command = f"terraform state mv '{match[0]}' '{match[1]}'"
        move_commands.append(command)

    # Write the move commands to the output file
    with open(output_path, 'w') as f:
        for command in move_commands:
            f.write(command + '\n')

    print(f"Terraform move commands have been written to {output_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Terraform Resource Matcher')
    parser.add_argument('--plan', required=True, help='Path to tfplan.json')
    parser.add_argument('--output', default='terraform_move_commands.sh',
                        help='Path to output file for terraform move commands')
    args = parser.parse_args()
    main(args.plan, args.output)
