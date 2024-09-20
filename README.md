# terraform-move-helper

**terraform-move-helper** is a CLI tool designed to automate the matching and migration of resources in Terraform plans. It intelligently pairs destroyed and created resources, generates `terraform state mv` commands, and helps streamline the migration of Terraform state during complex refactorings or infrastructure changes.


## Features

- **Intelligent Matching**: Uses heuristics, such as attribute similarity and address similarity, to match destroyed resources with created ones.
- **Move Command Generation**: Automatically generates `terraform state mv` commands for matched resources.
- **Unmatched Resource Reporting**: Identifies and lists resources that don't have a matching counterpart.

## **Use Case Example: Refactoring Terraform Infrastructure**

**Scenario:**
Imagine you're managing a large Terraform codebase for your cloud infrastructure, and you're refactoring it to use modules or change resource naming conventions. For example, you're breaking up a monolithic set of resources into smaller, more manageable modules or updating resource names to follow a new naming standard.

During this refactor, many resources need to be renamed, or their configurations need to change in a way that requires Terraform to destroy the old resources and create new ones. However, the actual underlying resources in the cloud (like AWS S3 buckets, EC2 instances, or RDS databases) are the same, and you don't want to recreate these resources.

Terraform detects these changes as **"destroy"** and **"create"** operations. However, if you don't want the resources to be physically destroyed and recreated in the cloud, you can use the `terraform state mv` command to move the state from the old resource to the new resource.

**Problem:**
- You have hundreds of resources, and many of them are being renamed or moved to new modules.
- Manually determining which destroyed resources match which created resources and writing the `terraform state mv` commands for each pair can be tedious and error-prone.

**Solution:**
**terraform-move-helper** automates this process by:
1. **Parsing the Terraform plan** to find resources that are being destroyed and created.
2. **Matching destroyed resources with created resources** using heuristics (like attribute and name similarity).
3. **Generating `terraform state mv` commands** to move the state from the old resource to the new one, preventing unnecessary destruction and recreation of resources.

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
- terraform-move-helper will automatically match the destroyed and created resources based on their attributes (e.g., bucket name) and generate the appropriate `terraform state mv` command for you:
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

---

This tool is especially useful for teams or individuals managing large-scale Terraform infrastructure, where maintaining state consistency during refactors is critical.



## Installation

### Prerequisites

- Python 3.7+
- Install required Python dependencies by running:

```bash
pip install -r requirements.txt
```

### Clone the Repository

```bash
git clone https://github.com/ttauveron/terraform-move-helper.git
cd terraform-move-helper
```

### Setting Up the Environment

You can create a virtual environment to manage dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

terraform-move-helper processes a Terraform plan in JSON format, matches destroyed and created resources, and generates `terraform state mv` commands. 

### Command Line Usage

To use terraform-move-helper, run the following command:

```bash
python terraform-move-helper.py --plan <path_to_tfplan.json> --output <output_file>
```

To generate the tfplan.json file, run the following command in your terraform project:

```bash
terraform plan -out=tfplan; terraform show -json tfplan | jq  > tfplan.json
```

### Example

```bash
python terraform-move-helper.py --plan tfplan.json --output move_commands.sh
```

This will:
- Parse the `tfplan.json` file.
- Match destroyed and created resources.
- Generate `terraform state mv` commands and write them to `move_commands.sh`.

### Output

1. **Matched Resources**:
   - The tool outputs matched resources with a similarity score.
   - Generates `terraform state mv` commands.

2. **Unmatched Resources**:
   - If there are unmatched resources (destroyed or created), they will be listed.

3. **Move Command Output**:
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
Total Similarity Score: 0.85

Unmatched Destroyed Resources:
 - module.files["test2"].local_file.default

Unmatched Created Resources:
 - module.files["test3"].local_file.default

Terraform move commands have been written to move_commands.sh
```

## Development

### Testing

To test the functionality with sample data:

1. Create or obtain a sample `tfplan.json` using:

    ```bash
    terraform show -json > tfplan.json
    ```

2. Run terraform-move-helper with the sample plan:

    ```bash
    python terraform-move-helper.py --plan tfplan.json --output move_commands.sh
    ```

### Contributing

1. Fork the repository.
2. Create a new branch (`git checkout -b feature-branch`).
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature-branch`).
5. Create a new Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
