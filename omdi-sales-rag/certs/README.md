# Optional corporate root certificates

If HTTPS traffic is inspected by a trusted company firewall or proxy, place its
public PEM-encoded root certificate in this directory with a `.crt` extension, for
example:

```text
certs/company-root-ca.crt
```

The Docker image adds any `.crt` files found here to its system trust store. Never
place a private key in this directory. Certificate files are ignored by Git and
should be managed separately for each deployment.
