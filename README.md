# TWsp_MonsMgr repository structure

- `app/` : Android project ZIP only.
- `tools/altema-master/` : Altema 659-monster baseline rebuild tool and comparison CSV.
- `.github/workflows/build-apk.yml` : builds only ZIPs under `app/`.
- `.github/workflows/build-altema-master.yml` : runs only the Altema rebuild tool.

## Separation rule
APK workflow never scans `tools/`.
Altema workflow never scans `app/`.
Therefore both can coexist without deleting files between runs.

## App update
Place the newest `TerrySPMonsterManager*.zip` in `app/`.
The APK workflow selects the newest filename by version sort.

## Altema rebuild
Keep `tools/altema-master/current_monsters.csv` as the comparison baseline.
Run `Rebuild Altema Master 659` from GitHub Actions.
