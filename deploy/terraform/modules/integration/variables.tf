variable "project" {
  type        = string
  description = "GCP project id (Datastore, Pub/Sub, KMS and Cloud Run live here)."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name" {
  type        = string
  description = "Integration name, used for resource names (e.g. \"hubspot\")."
}

variable "image" {
  type        = string
  description = "Integration image (docker/integration.Dockerfile), ideally pinned by digest."
}

variable "public_base_url" {
  type        = string
  description = "Public URL of the API service (custom domain) that Dialpad and the iframe use."
}

variable "connector" {
  type        = string
  description = "CONNECTOR entry-point name."
}

variable "features" {
  type        = list(string)
  description = "FEATURES entry-point names, e.g. [\"call_logging\", \"sms_logging\"]."
}

variable "kms_key" {
  type        = string
  description = "Cloud KMS key id (projects/.../cryptoKeys/...) wrapping the data keys."
}

variable "cache_url" {
  type        = string
  description = "Redis URL (Memorystore), e.g. redis://10.0.0.3:6379/0."
}

variable "vpc_connector" {
  type        = string
  default     = null
  description = "Serverless VPC Access connector for reaching Memorystore (null: no VPC egress)."
}

variable "env" {
  type        = map(string)
  default     = {}
  description = "Extra non-secret settings (e.g. CALL_LOGGING_SKIP_STATES, HUBSPOT_CLIENT_ID)."
}

variable "secret_env" {
  type        = map(string)
  description = <<-EOT
    Secret settings: env var name -> Secret Manager secret id. Must include DIALPAD_CLIENT_SECRET,
    WEBHOOK_SECRET_KEY and SESSION_SECRET, plus the connector's (e.g. HUBSPOT_CLIENT_SECRET).
  EOT
  validation {
    condition = alltrue([
      for k in ["DIALPAD_CLIENT_SECRET", "WEBHOOK_SECRET_KEY", "SESSION_SECRET"] : contains(keys(var.secret_env), k)
    ])
    error_message = "secret_env must define DIALPAD_CLIENT_SECRET, WEBHOOK_SECRET_KEY and SESSION_SECRET."
  }
}

variable "api_instances" {
  type    = object({ min = number, max = number })
  default = { min = 1, max = 20 }
}

variable "worker_instances" {
  type        = number
  default     = 2
  description = "Always-on worker instances (they pull every shard; Pub/Sub balances the load)."
}

variable "deletion_protection" {
  type    = bool
  default = true
}
