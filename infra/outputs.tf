output "api_id" {
  value = aws_apigatewayv2_api.http.id
}

output "api_endpoint" {
  description = "Base URL on AWS. The webhook path is /webhooks/stripe."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "table_name" {
  value = aws_dynamodb_table.orders.name
}

output "orders_queue_url" {
  value = aws_sqs_queue.orders.url
}

output "dead_letter_queue_url" {
  value = aws_sqs_queue.dead_letter.url
}

output "webhook_function" {
  value = aws_lambda_function.webhook.function_name
}

output "consumer_function" {
  value = aws_lambda_function.consumer.function_name
}
