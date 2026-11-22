# Example: the HubSpot integration with call and SMS logging.
#   terraform init && terraform apply -var image=REGION-docker.pkg.dev/PROJECT/integrations/hubspot@sha256:...

variable "image" {
  type = string
}

module "hubspot" {
  source = "../../modules/integration"

  project         = "my-gcp-project"
  region          = "us-central1"
  name            = "hubspot"
  image           = var.image
  public_base_url = "https://hubspot.integrations.example.com"
  connector       = "hubspot"
  features        = ["hubspot.calls", "hubspot.sms", "hubspot.notes"]
  kms_key         = "projects/my-gcp-project/locations/us-central1/keyRings/integrations/cryptoKeys/hubspot"
  cache_url       = "redis://10.0.0.3:6379/0"
  vpc_connector   = "projects/my-gcp-project/locations/us-central1/connectors/integrations"

  env = {
    HUBSPOT_CLIENT_ID       = "your-hubspot-app-client-id"
    DIALPAD_CLIENT_ID       = "your-dialpad-app-client-id"
    DIALPAD_IFRAME_URL      = "https://hubspot.integrations.example.com/app/"
    CALL_LOGGING_RECORDINGS = "true"
  }
  secret_env = {
    DIALPAD_CLIENT_SECRET = "hubspot-dialpad-client-secret"
    WEBHOOK_SECRET_KEY    = "hubspot-webhook-secret-key"
    SESSION_SECRET        = "hubspot-session-secret"
    HUBSPOT_CLIENT_SECRET = "hubspot-client-secret"
  }
}

output "api_uri" {
  value = module.hubspot.api_uri
}
