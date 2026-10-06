# The stock backend-plugin image build needs bitcart/bitcart:stable, which a pinned
# BITCART_VERSION deployment never has, so the coin registration module is mounted instead.
# Package is "forkedton", not "forked": the Solana plugin already mounts /app/modules/forked.
# Append only: other plugins' rules own the remaining entries, and rule order is undefined.
MODULES_MOUNT = "./plugins/docker/ton/backend/forkedton:/app/modules/forkedton:ro"


def rule(services, settings):
    for name in ("backend", "worker"):
        if name in services:
            services[name].setdefault("volumes", []).append(MODULES_MOUNT)
