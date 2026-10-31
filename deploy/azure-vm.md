# Deploying the warehouse on Azure and running the non-npm crawl

This runbook deploys one Ubuntu VM, runs PostgreSQL in Docker on it, and
collects the six non-npm ecosystems:

- NuGet (HTTP)
- PyPI (HTTP)
- Packagist (headless Selenium)
- Hex (headless Selenium)
- RubyGems (headless Selenium)
- crates.io (headless Selenium)

npm is deliberately left out. It needs a headed browser and a residential IP
that Cloudflare trusts, so it is better run from your own machine against the
same database (optional step 11).

The scripts assume you run everything from the repository root unless a step
says otherwise.

---

## 1. Check the subscription before creating anything

Azure for Students usually gives:

- 100 USD credit for 12 months, no credit card, no overage billing
- about 4 vCPUs total in the subscription
- a restricted list of allowed regions
- no quota increases

A `Standard_B2s_v2` VM (2 vCPU, 8 GB) fits inside that quota and is the deployable
choice for this subscription (the plain `Standard_B2s` was rejected with
`NotAvailableForSubscription` in Poland Central).

### Portal checks

1. **Subscriptions → Usage + quotas**
   Look for `Total Regional vCPUs` and `Standard BS Family vCPUs`. You need at
   least 2 free vCPUs in the region you pick.
2. **Azure Policy → Assignments**
   Find the assignment named like “Allowed resource deployment regions” and
   note the permitted locations. If you deploy outside them, the create call
   fails with a policy error.
3. **Cost Management + Billing → Overview**
   Confirm the remaining credit. A B2s costs roughly 1 USD/day while running,
   so the credit covers months, not hours.

### CLI checks

Install the CLI on Arch:

```bash
sudo pacman -S azure-cli
az login
# on a machine without a browser:
# az login --use-device-code
```

Then:

```bash
az account show --query "{name:name,id:id,state:state}" -o table

# pick a region that the policy allows
LOCATION=swedencentral

# vCPU usage and limits in that region
az vm list-usage --location "$LOCATION" -o table

# check the deployable sizes for this subscription
az vm list-skus --location "$LOCATION" \
  --resource-type virtualMachines --size Standard_B2s_v2 --all -o table

# policy assignments visible at the subscription scope
az policy assignment list \
  --query "[].{name:name,displayName:displayName,scope:scope}" -o table
```

If the policy list is empty, the restriction is probably assigned at a
management group and is easier to read in the portal.

The confirmed `sys.regionrestriction` assignment for this subscription
(“Allowed resource deployment regions”) allows only these regions:

  swedencentral, norwayeast, francecentral, spaincentral, switzerlandnorth

Poland Central is not on the list, which is why the VM deployment was rejected
with `RequestDisallowedByAzure`. Deploy into one of the allowed regions —
`swedencentral` is the closest pick.

---

## 2. Create the resource group and VM

```bash
RG=hurtownie-rg
VM=hurtownie-vm
LOCATION=swedencentral

az group create --name "$RG" --location "$LOCATION"

az vm create \
  --resource-group "$RG" \
  --name "$VM" \
  --location "$LOCATION" \
  --image Ubuntu2404 \
  --size Standard_B2s_v2 \
  --storage-sku StandardSSD_LRS \
  --os-disk-size-gb 64 \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --nsg-rule SSH
```

`--nsg-rule SSH` opens only port 22. Do not open 5432 or 55432.

Get the address:

```bash
IP=$(az vm show --resource-group "$RG" --name "$VM" \
  --show-details --query publicIps -o tsv)
echo "$IP"
```

Check that SSH works:

```bash
ssh azureuser@"$IP" 'uname -a && df -h /'
```

A freshly created VM may take a minute before SSH accepts connections.

---

## 3. Copy the project to the VM

Do this from your laptop, in the repository root. No commit or push is needed.

```bash
rsync -av --progress \
  --exclude .venv \
  --exclude .env \
  --exclude output \
  --exclude .git \
  --exclude '__pycache__' \
  ./ "azureuser@$IP:~/hurtownie-projekt/"
```

---

## 4. Bootstrap the VM

```bash
ssh azureuser@"$IP"
cd ~/hurtownie-projekt
bash deploy/bootstrap.sh
```

The script installs Docker, Docker Compose v2, Google Chrome, `rsync`, `tmux`
and `uv`. It adds your user to the `docker` group.

Log out and back in so the group takes effect:

```bash
exit
ssh azureuser@"$IP"
```

Verify:

```bash
docker --version
docker compose version
google-chrome --version
uv --version
```

If `uv` is not on `PATH`, use `~/.local/bin/uv` or run:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

---

## 5. Create the Python environment

```bash
cd ~/hurtownie-projekt
uv venv .venv
uv pip install -e '.[dev]'
.venv/bin/python -c "import scrapy, selenium, psycopg; print('deps ok')"
```

---

## 6. Configure the environment

```bash
cp deploy/azure.env.example .env
set -a
source .env
set +a
```

The application does **not** read `.env` by itself. Every new shell or tmux
session must source it. The important values are:

```dotenv
WAREHOUSE_DSN=postgresql://warehouse:warehouse@localhost:55432/warehouse
BROWSER_BINARY=/usr/bin/google-chrome
BROWSER_HEADLESS=true
```

The scale script also passes `BROWSER_HEADLESS=true` to the four Selenium
spiders, but setting it here keeps manual pilot runs consistent.

---

## 7. Start PostgreSQL and create the schema

```bash
docker compose up -d
docker compose ps
```

Wait for `healthy`, then:

```bash
.venv/bin/python -m warehouse.db init
```

The compose file binds PostgreSQL to `127.0.0.1:55432` only, so it is not
reachable from the internet even if the network security group is changed by
mistake.

---

## 8. Smoke-test both collection paths

Run a tiny PyPI pilot (HTTP path):

```bash
PYPI_POPULAR_COUNT=3 PYPI_RANDOM_COUNT=0 PYPI_MAX_VERSIONS=2 \
  .venv/bin/scrapy crawl pypi 2>&1 | tee /tmp/pypi-pilot.log
```

Run a tiny Packagist pilot (headless Chrome path):

```bash
BROWSER_HEADLESS=true \
PACKAGIST_POPULAR_COUNT=2 PACKAGIST_RANDOM_COUNT=0 PACKAGIST_MAX_VERSIONS=1 \
  .venv/bin/scrapy crawl packagist 2>&1 | tee /tmp/packagist-pilot.log
```

Check that each stored something:

```bash
grep -E "item_scraped_count|storage/stored|Spider closed" /tmp/*-pilot.log
```

Expected: `item_scraped_count` greater than 0 and `Spider closed (finished)`.

If the Packagist pilot fails with a Chrome error, check:

```bash
google-chrome --version
.venv/bin/python scripts/check_browser.py
```

---

## 9. Run the full non-npm collection

Run it inside tmux so an SSH disconnect does not kill the crawl:

```bash
tmux new -s warehouse
```

Inside the session:

```bash
cd ~/hurtownie-projekt
set -a
source .env
set +a

RUN_DIR="output/scale-non-npm-$(date +%Y%m%d-%H%M%S)"

SCALE_RUN_DIR="$RUN_DIR" \
SCALE_ECOSYSTEMS="nuget pypi packagist hex rubygems crates" \
SCALE_TIMEOUT_HTTP=21600 \
SCALE_TIMEOUT_BROWSER=43200 \
  scripts/scale_collection.sh
```

Key points:

- `SCALE_ECOSYSTEMS` must list the six ecosystems explicitly. The default
  includes npm, which would start a headed browser and fail on a server.
- `SCALE_TIMEOUT_HTTP=21600` is 6 hours per HTTP ecosystem.
- `SCALE_TIMEOUT_BROWSER=43200` is 12 hours per Selenium ecosystem. The default
  is 6 hours; crates.io in particular can exceed that. Raise it if a run is
  cut off.
- The script runs ecosystems sequentially, then OSV ingestion and matching for
  each, then the analysis, report and dashboard.
- The target is 2,000 popular + 500 random packages per ecosystem and 5 sampled
  versions per package. Override with `SCALE_POPULAR_COUNT`,
  `SCALE_RANDOM_COUNT`, `SCALE_MAX_VERSIONS` and `SCALE_RANDOM_PAGES` if you
  want a smaller first pass.

Detach with `Ctrl+B`, then `D`. Reattach with:

```bash
tmux attach -t warehouse
```

Expect this to take **one to several days**, mostly in the Selenium ecosystems.

---

## 10. Monitor progress

From a second SSH session:

```bash
cd ~/hurtownie-projekt
scripts/scale_status.sh
tail -f output/scale-non-npm-*/driver.log
```

Per-spider result table:

```bash
column -t -s $'\t' output/scale-non-npm-*/summary.tsv
```

`status` is `succeeded`, `partial`, `failed` or `timeout`. A timeout is not
fatal: data stored before the cut-off is kept, and you can resume.

Quick database counts:

```bash
docker compose exec -T postgres psql -U warehouse -d warehouse -c "
SELECT e.name, count(DISTINCT p.id) AS packages,
       count(v.id) AS versions
FROM package p
JOIN ecosystem e ON e.id = p.ecosystem_id
LEFT JOIN package_version v ON v.package_id = p.id
GROUP BY e.name
ORDER BY e.name;"
```

Resume after an interruption without repeating finished ecosystems:

```bash
SCALE_RUN_DIR="$RUN_DIR" \
SCALE_ECOSYSTEMS="packagist hex rubygems crates" \
SCALE_TIMEOUT_HTTP=21600 SCALE_TIMEOUT_BROWSER=43200 \
  scripts/scale_collection.sh
```

Analysis, report and dashboard always run over the whole database, not only the
ecosystems selected for that invocation.

---

## 11. Optional: run npm locally into the same database

This gives you all seven ecosystems in one place without exposing PostgreSQL.

On the VM, leave the database on loopback. On your laptop, open a tunnel on a
free local port:

```bash
ssh -N -L 55433:localhost:55432 azureuser@"$IP"
```

In another local terminal, in the repository root:

```bash
set -a
source .env          # your local env, with BROWSER_HEADLESS=false
set +a

WAREHOUSE_DSN=postgresql://warehouse:warehouse@localhost:55433/warehouse \
BROWSER_HEADLESS=false \
SCALE_RUN_DIR="output/npm-local-$(date +%Y%m%d-%H%M%S)" \
SCALE_ECOSYSTEMS=npm \
  scripts/scale_collection.sh
```

This uses your warmed Chrome profile at `~/.cache/hurtownie/chrome-profile`, so
Cloudflare sees the same client IP that already passed the challenge. The
script runs `warehouse.db init`, the npm spider, OSV ingestion, matching,
analysis, report and dashboard against the Azure database.

Do not run this while the local Docker PostgreSQL is also using port 55432; the
tunnel uses 55433 so both can coexist.

Afterwards, rebuild the dashboard once more on either side so it includes npm:

```bash
.venv/bin/python analysis/build_dashboard.py --output output/dashboard.html
```

---

## 12. Collect results and back up the database

On the VM:

```bash
cd ~/hurtownie-projekt
docker compose exec -T postgres pg_dump -U warehouse -d warehouse \
  | gzip > ~/warehouse-$(date +%F).sql.gz
```

Copy the run directory and dump to your laptop:

```bash
rsync -av "azureuser@$IP:~/hurtownie-projekt/output/scale-non-npm-*/" ./output/
rsync -av "azureuser@$IP:~/warehouse-*.sql.gz" ./output/
```

The interesting files in the run directory are:

- `summary.tsv` — per-spider status, exit code, timing
- `report.md` — scale report
- `dashboard.html` — self-contained dashboard
- `queries.txt` — analysis query output
- `<spider>.log` — full crawl log, including 403/429 details

---

## 13. Stop or remove the VM

Stop compute billing when finished:

```bash
az vm deallocate --resource-group "$RG" --name "$VM"
```

The OS disk and its PostgreSQL data remain and are still billed (a few dollars
per month). Start it again with:

```bash
az vm start --resource-group "$RG" --name "$VM"
```

The public IP may change after a deallocate/start cycle. Re-read it with the
`az vm show` command from step 2.

Delete everything, including the database disk:

```bash
az group delete --name "$RG" --yes --no-wait
```

Only do that after copying the dump and run outputs.

---

## 14. Troubleshooting

**`SkuNotAvailable` or a policy error on create**
Pick a region listed in the allowed-locations policy and check
`az vm list-skus` in that region. Student subscriptions cannot request quota
increases, so move regions instead.

**`permission denied` on the Docker socket**
The `docker` group change needs a new login. Exit the SSH session and reconnect,
or run `newgrp docker`.

**Chrome fails to start**
Installation of `google-chrome-stable` pulls the required libraries. Confirm
`google-chrome --version` works as your user, then run
`.venv/bin/python scripts/check_browser.py`. Keep `BROWSER_HEADLESS=true` for
the non-npm spiders.

**A spider stores zero items**
Read `output/.../<spider>.log`. HTTP 403/429 means the site is blocking the
datacenter IP. Increase `SCRAPY_DOWNLOAD_DELAY`, keep
`SCRAPY_CONCURRENT_PER_DOMAIN=1`, and rerun only that ecosystem.

**`timeout` in `summary.tsv`**
Raise `SCALE_TIMEOUT_BROWSER` (for example to 86400) and resume the remaining
ecosystems with the same `SCALE_RUN_DIR`.

**SSH disconnects**
The crawl keeps running if it was started in tmux. Reconnect and
`tmux attach -t warehouse`.

**Disk fills up**
`df -h`, then `docker system prune -af` removes unused images and build cache.
The warehouse data itself is small; Chrome and Docker images are the bulk.

**Credit exhausted**
The student subscription is disabled rather than billed. Deallocate the VM and
delete the resource group, or upgrade to pay-as-you-go if you want to continue.

---

## Cost expectation

| Item | Approximate cost |
| --- | --- |
| `Standard_B2s_v2` while running | ~1 USD/day |
| 64 GB Standard SSD OS disk | ~5 USD/month |
| Standard public IP | ~3 USD/month |
| Internet egress | first 100 GB/month free; text crawl uses far less |

The 100 USD credit is enough for a multi-week project. The expensive mistake is
leaving the VM running for months, not the crawl itself.
