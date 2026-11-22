Inference Gateway release, supplied by the Gateway project and carried UNCHANGED.

inference-gateway-<version>.zip and its .sha256 are copied byte for byte from the Gateway project's release folder. On the hub, the provisioner checks the archive against the .sha256, unpacks it to a local staging folder, verifies every file against SHA256SUMS and the deployment manifest, and then runs the Gateway's own commands in the manifest's order (Administration hub > Inference Gateway guided installation). Nothing of the Gateway is copied into Agent Arena.
