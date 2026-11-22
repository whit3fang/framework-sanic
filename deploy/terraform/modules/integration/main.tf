# One integration on Cloud Run: an API service (webhooks, iframe UI, OAuth) and an always-on
# worker service (queue consumers + schedules), sharing the framework's production adapters:
# Datastore, Pub/Sub, Memorystore Redis and Cloud KMS. Datastore indexes are deployed by CI
# (`framework-sanic export-indexes`), Pub/Sub topics and subscriptions by the service at startup.

locals {
  env = merge(
    {
      PLATFORM_ADAPTER  = "dialpad"
      CONNECTOR         = var.connector
      FEATURES          = join(",", var.features)
      STORAGE_ADAPTER   = "datastore"
      DATASTORE_PROJECT = var.project
      QUEUE_ADAPTER     = "pubsub"
      PUBSUB_PROJECT    = var.project
      PUBSUB_PREFIX     = var.name
      CACHE_URL         = var.cache_url
      KMS_KEY           = var.kms_key
      PUBLIC_BASE_URL   = var.public_base_url
      LOG_LEVEL         = "INFO"
    },
    var.env,
  )
  services = {
    api = {
      embedded_workers = "false"
      ingress          = "INGRESS_TRAFFIC_ALL"
      min              = var.api_instances.min
      max              = var.api_instances.max
      cpu_idle         = true
    }
    workers = {
      embedded_workers = "true"
      ingress          = "INGRESS_TRAFFIC_INTERNAL_ONLY"
      min              = var.worker_instances
      max              = var.worker_instances
      cpu_idle         = false # consumers and schedules run without requests
    }
  }
}

resource "google_service_account" "app" {
  project      = var.project
  account_id   = "${var.name}-integration"
  display_name = "${var.name} integration"
}

resource "google_project_iam_member" "app" {
  for_each = toset(["roles/datastore.user", "roles/pubsub.editor"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.app.email}"
}

resource "google_kms_crypto_key_iam_member" "app" {
  crypto_key_id = var.kms_key
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "app" {
  for_each  = var.secret_env
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_cloud_run_v2_service" "service" {
  for_each            = local.services
  project             = var.project
  location            = var.region
  name                = "${var.name}-${each.key}"
  ingress             = each.value.ingress
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.app.email
    scaling {
      min_instance_count = each.value.min
      max_instance_count = each.value.max
    }
    dynamic "vpc_access" {
      for_each = var.vpc_connector == null ? [] : [var.vpc_connector]
      content {
        connector = vpc_access.value
        egress    = "PRIVATE_RANGES_ONLY"
      }
    }
    containers {
      image   = var.image
      command = ["framework-sanic"]
      args    = ["serve", "--host", "0.0.0.0", "--port", "8080"]
      ports {
        container_port = 8080
      }
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = each.value.cpu_idle
      }
      env {
        name  = "EMBEDDED_WORKERS"
        value = each.value.embedded_workers
      }
      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
      liveness_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.app, google_kms_crypto_key_iam_member.app]
}

# Dialpad webhooks and the iframe reach the API publicly; requests are authenticated by the
# framework (signed webhooks, id_token, session tokens).
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.service["api"].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
