# Security and private data

The repository contains source code and example configuration only. Do not commit the generated `~/ai_setup/.env`, `.htpasswd`, TLS certificates or private keys, model files, ComfyUI inputs/outputs, benchmark databases, WebUI data, logs, or backups. `.gitignore` excludes common copies of these, but review `git status` before every push.

The installer generates API, WebUI, terminal, switcher, and CSRF secrets on the Ubuntu host. Retrieve them with `~/ai_setup/ai-control.sh` only when needed; do not paste them into issues, logs, or commits. The API gateway stores newly created client keys as hashes and displays each full key only once. Revoke a lost key in **Client sharing**.

The default API URL uses HTTP on a private LAN address. Bearer keys sent through HTTP are visible to anyone able to observe that traffic. If access is needed beyond a trusted LAN, place a properly configured HTTPS reverse proxy in front of the API and restrict firewall access. Do not expose the switcher, ComfyUI, Docker socket, or model backends directly to the internet.

The controller has access to the Docker socket so it can switch and update containers. Treat access to the switcher as administrative access to this host. Use the generated Basic Auth password and restrict network access to trusted operators.

If a credential is accidentally committed, revoke or rotate it on the Ubuntu host and remove it from the Git history. Making a repository private does not make committed secrets safe.
