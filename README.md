# terraform-move-helper

**terraform-move-helper** is a CLI tool for reviewing Terraform refactors that appear in a JSON plan as separate `delete` and `create` actions. It conservatively pairs destroyed and created resources, generates `terraform state mv` commands for confident matches, and reports weak or ambiguous candidates for manual review.


## Features

- **Conservative Matching**: Uses exact state fingerprints, unique identity attributes, weighted state similarity, and address context from accepted matches.
- **Confidence Checks**: Applies minimum score and ambiguity-margin checks before accepting a match.
- **Move Command Generation**: Generates shell-quoted `terraform state mv` commands for accepted matches.
- **Review-Oriented Output**: Prints match scores, match reasons, ambiguous candidates, and unmatched resources.

## **Use Case Example: Refactoring Terraform Infrastructure**

**Scenario:**
Imagine you're managing a large Terraform codebase for your cloud infrastructure, and you're refactoring it to use modules or change resource naming conventions. For example, you're breaking up a monolithic set of resources into smaller, more manageable modules or updating resource names to follow a new naming standard.

During this refactor, many resources need to be renamed, or their configurations need to change in a way that requires Terraform to destroy the old resources and create new ones. However, the actual underlying resources in the cloud (like AWS S3 buckets, EC2 instances, or RDS databases) are the same, and you don't want to recreate these resources.

Terraform detects these changes as **"destroy"** and **"create"** operations. However, if you don't want the resources to be physically destroyed and recreated in the cloud, you can use the `terraform state mv` command to move the state from the old resource to the new resource.

**Problem:**
- You have hundreds of resources, and many of them are being renamed or moved to new modules.
- Manually determining which destroyed resources match which created resources and writing the `terraform state mv` commands for each pair can be tedious and error-prone.

**Solution:**
**terraform-move-helper** helps with this process by:
1. **Parsing the Terraform plan** to find resources that are being destroyed and created.
2. **Matching destroyed resources with created resources** using state-first heuristics and confidence checks.
3. **Generating `terraform state mv` commands** for accepted matches, so you can review and run them manually.

---

### **Example**

Let's say you have the following resources in your existing Terraform configuration:

```hcl
resource "aws_s3_bucket" "old_name" {
  bucket = "my-infrastructure-bucket"
  acl    = "private"
}
```

You decide to move this resource to a new module with a new naming convention:

```hcl
module "storage" {
  source = "./modules/storage"

  bucket_name = "my-infrastructure-bucket"
}
```

When you run `terraform plan`, Terraform will detect this as a **destroy and create** action because the resource name and location have changed, even though the underlying bucket is the same.

**Without terraform-move-helper:**
- You would have to manually figure out that these two resources are essentially the same and write the command to move the state:
  ```bash
  terraform state mv 'aws_s3_bucket.old_name' 'module.storage.aws_s3_bucket.new_name'
  ```
- Doing this for every resource in a large refactor is time-consuming and error-prone.

**With terraform-move-helper:**
- terraform-move-helper can match the destroyed and created resources based on their state, such as the bucket name, and generate the appropriate `terraform state mv` command for review:
  ```bash
  terraform state mv 'aws_s3_bucket.old_name' 'module.storage.aws_s3_bucket.new_name'
  ```

This prevents Terraform from destroying the existing bucket and recreating it, saving time, avoiding downtime, and preventing potential data loss.

---

### **Other Use Cases**

1. **Splitting Resources into Modules:**
   - If you are refactoring a flat Terraform file into modules, terraform-move-helper helps match the old resources with their new module-based counterparts.

2. **Renaming Resources for Standardization:**
   - If you're standardizing resource names across your infrastructure (e.g., adding prefixes or suffixes to resource names), terraform-move-helper can match the old names with the new ones and move the state accordingly.

3. **Large Infrastructure Changes:**
   - In cases where multiple resources are being updated, renamed, or moved across environments or regions, terraform-move-helper can simplify the process by automating the matching and generation of `terraform state mv` commands.

---

### **Why It's Useful**

- **Time-Saving**: Automates the generation of `terraform state mv` commands, saving hours of manual work.
- **Accuracy**: Reduces the risk of human error when matching destroyed and created resources.
- **Prevents Resource Re-creation**: Helps avoid unnecessary resource destruction and recreation, preventing downtime and potential data loss.
- **Scalability**: Handles large infrastructure refactors, where manually matching resources would be impractical.
- **Safety**: Refuses to guess when candidates are weak or ambiguous.

---

This tool is especially useful for teams or individuals managing large-scale Terraform infrastructure, where maintaining state consistency during refactors is critical.



## Installation

### Prerequisites

- Python 3.9+
- [uv](https://docs.astral.sh/uv/)

### Clone the Repository

```bash
git clone https://github.com/ttauveron/terraform-move-helper.git
cd terraform-move-helper
```

### Setting Up the Environment

Create the virtual environment and install dependencies with uv:

```bash
uv sync
```

## Usage

terraform-move-helper processes a Terraform plan in JSON format, matches destroyed and created resources, and writes `terraform state mv` commands for confident matches.

Always review the generated commands before running them against real state.

### Command Line Usage

To use terraform-move-helper, run the following command:

```bash
uv run terraform-move-helper --plan <path_to_tfplan.json> --output <output_file>
```

To generate the tfplan.json file, run the following command in your terraform project:

```bash
terraform plan -out=tfplan
terraform show -json tfplan > tfplan.json
```

### Example

```bash
uv run terraform-move-helper --plan tfplan.json --output move_commands.sh
```

This will:
- Parse the `tfplan.json` file.
- Match destroyed and created resources.
- Generate `terraform state mv` commands and write them to `move_commands.sh`.

### Output

1. **Matched Resources**:
   - The tool outputs matched resources with a similarity score and match reason.
   - Match reasons include `exact fingerprint`, `unique identity`, `weighted state`, and `address context`.
   - Accepted matches are written as `terraform state mv` commands.

2. **Ambiguous Matches**:
   - If multiple candidates are too close, the tool prints the candidates and does not generate a command for that resource.

3. **Unmatched Resources**:
   - Resources without a counterpart, or with only low-confidence candidates, are listed.

4. **Move Command Output**:
   - The `terraform state mv` commands are saved to the specified output file.

### Handling Mismatches

If there is a mismatch between the number of destroyed and created resources for any resource type, terraform-move-helper will print an error and cancel execution:

```
Error: Mismatch for resource type 'aws_s3_bucket'
  Destroyed: 2 resource(s)
  Created: 1 resource(s)
Cannot proceed with matching because the numbers don't match.
```

## Options

- `--plan`: (Required) The path to the Terraform plan in JSON format.
- `--output`: (Optional) The path to the output file where `terraform state mv` commands will be written. Default: `terraform_move_commands.sh`.

### Example Output

```bash
Matched Destroyed Resource: module.files["test1"].local_file.default
With Created Resource: module.files["test1-aaa"].local_file.default
Total Similarity Score: 1.00
Match Reason: exact fingerprint

Ambiguous Matches:
Ambiguous match:
  destroyed: module.files["test2"].local_file.default
  candidates:
    - module.files["test2-a"].local_file.default score=0.82
    - module.files["test2-b"].local_file.default score=0.81

Unmatched Destroyed Resources:
 - module.files["test3"].local_file.default

Unmatched Created Resources:
 - module.files["test4"].local_file.default

Terraform move commands have been written to move_commands.sh
```

## Development

### Testing

Run the automated test suite with:

```bash
uv run --group dev pytest
```

To test the functionality manually with sample data:

1. Create or obtain a sample `tfplan.json` using:

    ```bash
    terraform plan -out=tfplan
    terraform show -json tfplan > tfplan.json
    ```

2. Run terraform-move-helper with the sample plan:

    ```bash
    uv run terraform-move-helper --plan tfplan.json --output move_commands.sh
    ```

### Contributing

1. Fork the repository.
2. Create a new branch (`git checkout -b feature-branch`).
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature-branch`).
5. Create a new Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
