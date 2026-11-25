variable "name" {
  description = "Globally unique bucket name."
  type        = string
}

variable "versioning" {
  description = "Keep old versions of overwritten or deleted objects. One-way: once true, AWS only accepts \"Suspended\", never back to \"Disabled\"."
  type        = bool
  default     = false
}

variable "noncurrent_days" {
  description = "Days to keep an old version (only with versioning)."
  type        = number
  default     = 90
}
