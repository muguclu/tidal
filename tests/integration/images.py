"""Container images for integration tests, pinned by digest (SPEC-06 §5).

Keep these equal to docker-compose.yml and .github/workflows/integration.yml;
bump all three together.
"""

SQL_SERVER_IMAGE = (
    "mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04"
    "@sha256:4402d880dd4c34bfa7d8705e56a86cd6c88da80a1f6bbbe741f999e76264a090"
)
AZURITE_IMAGE = (
    "mcr.microsoft.com/azure-storage/azurite:3.37.0"
    "@sha256:830430c1da1a2d537e08f3e6764dd1f5ae00cf0346bcaf625b968ec3f0971fd5"
)
