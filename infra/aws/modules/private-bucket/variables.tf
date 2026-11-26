variable "name" {
  description = "Globally unique bucket name."
  type        = string
}

variable "versioning" {
  description = "Keep old versions of overwritten or deleted objects. One-way: once true, AWS only accepts \"Suspended\", never back to \"Disabled\"."
  type        = bool
  default     = false
}

variable "data_allowed_principal_arns" {
  description = "Role ARNs (wildcards allowed) that alone may Get/Put/Delete objects under inputs/ and results/; everyone else is denied. Required (no default) so a forgotten argument is a plan error, not an unlocked bucket; pass [] explicitly for no lock. Role ARNs, not assumed-role session ARNs."
  type        = list(string)
}

variable "noncurrent_days" {
  description = "Days to keep an old version (only with versioning)."
  type        = number
  default     = 90
}
