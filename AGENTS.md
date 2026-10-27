# LanScreen public source workspace

- Server sources are in server/, Android sources in android/, public documentation in docs/.
- Keep runtime databases, media, logs, local settings and signing private keys out of the repository.
- Put generated tests/build outputs in ignored work/ or android/build/ paths.
- Keep synthetic tests separate from production data. Do not run tests against a deployed data volume.
- Changes to deployment examples do not authorize production deployment or data migration.
- Record and stop temporary test/build processes when validation is complete.
- Preserve signing identity for compatible Android upgrades; never commit a signing keystore or its password.
