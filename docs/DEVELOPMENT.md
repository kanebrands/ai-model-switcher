# Development

Python 3.12 is used by the controller and gateway containers. The standard-library test suite has no third-party Python dependencies.

```bash
python3 -m unittest discover -s . -p 'test_*.py' -q
python3 build_installer.py
bash -n ai-setup.sh
bash -n install_models.sh start-selected.sh ai-control.sh
sh -n tune-context.sh tune-parallel.sh update-engine.sh update-webui.sh
```

`ai-setup.base.sh` is the original installer framework. Edit the individual source modules and `docker-compose.yml`, then run `build_installer.py` to regenerate `ai-setup.sh`. Commit the generated installer together with its source. The build must leave the committed installer unchanged when rerun.

The installer writes modules under `~/ai_setup/gpu_control` on the Ubuntu host. `install_profile.py` creates the hardware-filtered selection menu and records the explicit choices in `.env`. `install_models.sh` downloads only those choices; `start-selected.sh` creates them and starts the first chosen text or vision model. `server.py` serves the switcher UI and management actions. `model_catalog.py` and `catalog_discovery.py` handle GPU model suggestions and installation. `benchmarks.py` and `benchmark_views.py` handle measurements and history. `context_tuning.py`, `parallel_tuning.py`, and their shell helpers test and persist model settings. `sharing.py`, `sharing_views.py`, and `fairness_gateway.py` implement client keys and API admission. `gpu_metrics.py` reads NVIDIA counters when available.

Tests run without Docker or a GPU by mocking external actions. `test_install_profile.py` covers the CPU-only menu, multi-selection, no implicit choice, and selected Compose startup. The streaming gateway test binds local loopback ports; a restrictive sandbox may skip it. Before deploying changes to a live host, verify Compose and Nginx syntax, back up `.env` and the results database, and run a short real generation request through Nginx. Actual model slot, context, and engine changes can interrupt active requests.

The root `docker-compose.yml` and `nginx.conf` are source templates for the installer. The installer adjusts optional TLS mounts when the host has no custom TLS listener. Avoid committing a host's live `.env`, Nginx certificates, password file, results database, or downloaded models.
