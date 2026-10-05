# Extra CA certificates

If you are behind a TLS-inspecting proxy/firewall, put its root CA here as `*.crt` (PEM).
The container appends it to the trusted bundle at startup. Files here are git-ignored.
