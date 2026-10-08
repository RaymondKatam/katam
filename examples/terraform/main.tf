# Terraform against the Katam API using the generic REST provider.
#   export TF_VAR_katam_token=katam_xxx
terraform {
  required_providers {
    restapi = { source = "Mastercard/restapi", version = "~> 1.19" }
  }
}
variable "katam_token" { sensitive = true }

provider "restapi" {
  uri                   = "https://katam.lab.local"
  write_returns_object  = false
  create_returns_object = false
  id_attribute          = "name"
  headers = { Authorization = "Bearer ${var.katam_token}", "Content-Type" = "application/json" }
}

resource "restapi_object" "web" {
  path         = "/api/v1/vms"
  read_path    = "/api/v1/vms/{id}"
  destroy_path = "/api/v1/vms/{id}"
  object_id    = "tf-web-01"
  data = jsonencode({
    name = "tf-web-01", project = "default", template = "ubuntu-small",
    ip = "172.20.20.71/24", secgroups = ["default"]
  })
  ignore_all_server_changes = true
}

resource "restapi_object" "web_sg" {
  path          = "/api/v1/security-groups"
  create_path   = "/api/v1/security-groups/{id}"
  create_method = "PUT"
  update_path   = "/api/v1/security-groups/{id}"
  update_method = "PUT"
  read_path     = "/api/v1/security-groups"
  object_id     = "web"
  data = jsonencode({
    rules = [
      { direction = "ingress", protocol = "tcp", ports = "443", remote = "0.0.0.0/0" },
      { direction = "egress", protocol = "any", remote = "0.0.0.0/0" }
    ]
  })
}
