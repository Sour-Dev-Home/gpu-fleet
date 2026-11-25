# tflint: Terraform's own rules (unused declarations, missing version constraints, naming).
# The bundled ruleset needs no plugin download. The AWS plugin (invalid instance types and the like)
# adds little for S3-only code; revisit when compute or IAM grows.
config {
  call_module_type = "local" # also lint infra/aws/modules through the stacks that call them
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
