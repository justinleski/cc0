#!/bin/sh
set -eu

docker compose up --build -d

frontend_address=$(docker compose port frontend 3000)
api_address=$(docker compose port api 8000)
rustfs_console_address=$(docker compose port rustfs 9001)

printf '\nCC0 local stack\n'
printf '  App:             http://%s\n' "$frontend_address"
printf '  Swagger API docs: http://%s/docs\n' "$api_address"
printf '  API readiness:   http://%s/api/v1/ready\n' "$api_address"
printf '  RustFS console:  http://%s\n' "$rustfs_console_address"
printf '\nUseful commands\n'
printf '  Check services:  docker compose ps\n'
printf '  Follow logs:     docker compose logs -f api frontend\n'
printf '  Stop services:   docker compose stop\n'
printf '\nMore help: docs/README.md and docs/local-development.md\n'
