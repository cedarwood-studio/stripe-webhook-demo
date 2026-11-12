# Source this to point the AWS CLI, Terraform and boto3 at a local AWS Emulator.
# EMULATOR_HOME is where the emulator writes its per-run keys and certificate.
: "${EMULATOR_PORT:=9297}"
: "${EMULATOR_HOME:=$PWD/.emulator}"
_dir="$EMULATOR_HOME/.aws/emulator/$EMULATOR_PORT"
export AWS_ENDPOINT_URL="https://127.0.0.1:$EMULATOR_PORT"
export AWS_SHARED_CREDENTIALS_FILE="$_dir/emulator_credentials"
export AWS_CONFIG_FILE="$_dir/config"
export AWS_CA_BUNDLE="$_dir/ca.pem"
export AWS_EC2_METADATA_DISABLED=true
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE _dir
