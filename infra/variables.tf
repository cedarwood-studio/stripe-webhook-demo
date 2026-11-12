variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "stripe-webhook-demo"
}

variable "stripe_webhook_secret" {
  description = "Stripe endpoint signing secret (whsec_...). The demo only ever uses a made-up one."
  type        = string
  sensitive   = true
}

variable "max_receive_count" {
  description = "Deliveries of an order message before SQS moves it to the dead-letter queue."
  type        = number
  default     = 3
}

variable "visibility_timeout_seconds" {
  description = "How long a received order message stays hidden. Must be at least the consumer timeout."
  type        = number
  default     = 60
}
