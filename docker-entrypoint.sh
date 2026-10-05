#!/bin/sh
# Optional extra CA certificates (corporate TLS-inspecting proxies): put *.crt / *.pem into ./certs
set -e
extra=""
for f in /certs/*.crt /certs/*.pem; do
  [ -f "$f" ] && extra="$extra $f"
done
if [ -n "$extra" ]; then
  bundle=/tmp/ca-bundle.pem
  cat "$(python -c 'import certifi; print(certifi.where())')" $extra > "$bundle"
  export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle" HTTPLIB2_CA_CERTS="$bundle"
  echo "Using extra CA certificates:$extra"
fi
exec "$@"
