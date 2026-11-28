# AWS setup checklist (P5B)

Do these steps by hand, in this order. Exact values are in `code`. The JSON files to paste are in [deploy/iam/](../../deploy/iam/) and [deploy/s3-cors.json](../../deploy/s3-cors.json); replace the `<PLACEHOLDERS>` before pasting.

**Never create an AWS Organization, Control Tower, or IAM Identity Center: any of them ends the Free plan credits.** No AWS access key is created anywhere in this checklist. The server uses its instance role, and GitHub uses OIDC.

Placeholders you will fill in along the way:

| Placeholder | Where it comes from | Example |
|---|---|---|
| `<BUCKET>` | step 2, your choice | `pagecite-prod-k3x9q2` |
| `<ACCOUNT_ID>` | console, top-right menu | `123456789012` |
| `<INSTANCE_ID>` | step 6 | `i-0abc123def4567890` |
| `<EIP>` | step 7 | `13.200.10.20` |

## 0. OpenAI (dashboard)

1. Create a project: Settings → Projects → **Create project**, name `pagecite-prod`.
2. In that project, create an API key named `pagecite-prod-server`. Copy it straight into the server `.env` in step 8. Don't paste it anywhere else.
3. Set the project's **monthly budget**, for example `$15`, with an email alert. That's the backstop behind the API's own $0.50/day cap.
4. Optionally, limit the project's allowed models to `gpt-5.4-mini`, `gpt-5.4-nano` and `text-embedding-3-small`.

## 1. Billing

Keep your existing **$25/month** AWS budget. Expected spend is about $22/month (see the README cost table).

## 2. S3 bucket (region **ap-south-1**)

1. S3 → **Create bucket**:
   - **Bucket name:** `<BUCKET>`. Lowercase, **no dots**, because virtual-hosted TLS needs that.
   - **Region:** `Asia Pacific (Mumbai) ap-south-1`.
   - **Object Ownership:** `ACLs disabled (recommended)`.
   - **Block all public access:** **ON** (all 4 boxes).
   - **Bucket Versioning:** `Disable`.
   - **Default encryption:** `SSE-S3`, Bucket Key `Enable`.
2. Bucket → Permissions:
   - **Bucket policy:** paste [deploy/iam/s3-bucket-policy.json](../../deploy/iam/s3-bucket-policy.json). It denies every non-TLS request.
   - **CORS:** paste [deploy/s3-cors.json](../../deploy/s3-cors.json).
     - Origin `https://pagecite.duckdns.org`.
     - Method `PUT`.
     - Headers exactly `content-type` and `x-amz-checksum-sha256`: the two the presigned PUT signs. A unit test keeps this file equal to the code.
3. Bucket → Management → **Lifecycle rules**. Create two, matching [deploy/iam/s3-lifecycle.json](../../deploy/iam/s3-lifecycle.json):
   - `expire-backups-after-7-days`: prefix `backups/`, expire current versions after `7` days.
   - `abort-incomplete-uploads`: whole bucket, delete incomplete multipart uploads after `1` day.

## 3. IAM role for the instance: `pagecite-ec2`

IAM → Roles → **Create role**:

- **Trusted entity:** `AWS service` → `EC2`.
- **Permissions:**
  - attach the AWS-managed policy **`AmazonSSMManagedInstanceCore`** (Session Manager and deploys through Run Command)
  - add an **inline policy** named `pagecite-s3`: paste [deploy/iam/ec2-role-policy.json](../../deploy/iam/ec2-role-policy.json) with `<BUCKET>` filled in. It allows:
    - `GetObject`/`PutObject`/`DeleteObject` on `tenant/*`
    - `PutObject` on `backups/*`; no List or Delete there, because the lifecycle rule handles retention
    - `ListBucket` on the bucket (see the note below)
- **Role name:** `pagecite-ec2`.

> **Why `s3:ListBucket`:** without it, S3 answers a request for an object that doesn't exist with 403, not 404. Then `POST /documents/:id/complete` before the upload finished would be a 503 instead of `409 NOT_UPLOADED`, and a missing object in the parse worker would look transient instead of permanent. It lets the box list key *names* in this one bucket, nothing more. If you prefer to keep the role to `tenant/*` + `backups/*` only, delete that statement: everything works except those two error codes.

## 4. Security group: `pagecite-web` (VPC: default)

- **Inbound:**
  - `HTTP` TCP `80` from `0.0.0.0/0` and from `::/0` (Let's Encrypt HTTP-01 + redirect to HTTPS)
  - `HTTPS` TCP `443` from `0.0.0.0/0` and from `::/0`
  - **No rule for port 22.** Deploys and your shell both go through SSM.
- **Outbound:** the default (all).

Optionally, for HTTP/3, add `Custom UDP` `443` from `0.0.0.0/0` and `::/0`. Caddy offers it, but everything works over TCP without it.

## 5. (No key pair needed)

Session Manager gives you a shell without SSH. If you want an emergency SSH path anyway:
1. Create an `ED25519` key pair `pagecite-emergency` and keep the `.pem` offline.
2. Only when needed, add an inbound `22` rule from **your IP/32** and delete it afterwards. Password login is off on Ubuntu cloud images, so SSH is key-only.

## 6. EC2 instance

EC2 → **Launch instance**:

- **Name:** `pagecite`.
- **AMI:** `Ubuntu Server 24.04 LTS (HVM), SSD Volume Type`, 64-bit (x86).
- **Instance type:** `t3.small`.
- **Key pair:** `Proceed without a key pair`, or `pagecite-emergency`.
- **Network:**
  - default VPC and a public subnet
  - **Auto-assign public IP:** Enable; the Elastic IP replaces it in step 7
  - **Security group:** select existing `pagecite-web`
- **Storage:** `20` GiB `gp3`, Delete on termination: yes.
- **Advanced details:**
  - **IAM instance profile:** `pagecite-ec2`
  - **Metadata accessible:** Enabled
  - **Metadata version:** `V2 only (token required)`
  - **Metadata response hop limit:** **`2`**. The containers run behind Docker's bridge, one network hop past the host. With the default `1`, the API and the worker can't get the role's credentials, and every S3 call fails.

Launch, then write down `<INSTANCE_ID>`.

## 7. Elastic IP

EC2 → Elastic IPs → **Allocate** (Network Border Group `ap-south-1`) → **Associate** with instance `pagecite`. Write down `<EIP>`.

## 8. DuckDNS

At <https://www.duckdns.org> (sign in), add the domain `pagecite` → current ip `<EIP>` → **update ip**. Check from your laptop: `nslookup pagecite.duckdns.org` returns `<EIP>`. The address is static, so no DuckDNS updater is needed on the server.

## 9. Server: bootstrap and `.env` (Session Manager)

1. EC2 → instance `pagecite` → **Connect** → **Session Manager** → Connect. If the tab says the instance isn't connected yet, wait 2–3 minutes after launch. Also check the instance role and the hop limit.
2. In the browser shell:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/Hritik456/pagecite/main/deploy/bootstrap.sh | sudo bash
   sudo nano /opt/pagecite/.env      # fill in every <...> (see below), save
   sudo chmod 600 /opt/pagecite/.env
   free -m && swapon --show          # Swap: 2047
   ```

   `.env` values (the template is [deploy/env.prod.example](../../deploy/env.prod.example)):
   - **Fixed:**
     - `SITE_ADDRESS=pagecite.duckdns.org`
     - `S3_BUCKET=<BUCKET>`
     - `AWS_REGION=ap-south-1`
   - **Generated:** `SECRET_KEY`, `MYSQL_PASSWORD` and `MYSQL_ROOT_PASSWORD`, with `openssl rand -hex 24` (run it in the same shell).
   - **Secret:** `OPENAI_API_KEY`, the project key from step 0. It is the only secret you type.
   - **Leave as in the template:** `DEMO_ASK_KEY` empty for now (step 12), and the limits (`10` / `20` / `0.50`).

## 10. GitHub OIDC → AWS deploy role

1. IAM → Identity providers → **Add provider**:
   - type `OpenID Connect`
   - URL `https://token.actions.githubusercontent.com`
   - audience `sts.amazonaws.com`
2. IAM → Roles → **Create role**:
   - **Trusted entity:** `Web identity`, provider `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`, GitHub organization `Hritik456`, repository `pagecite`, branch `main`.
   - **Trust policy:** replace it with [deploy/iam/deploy-role-trust.json](../../deploy/iam/deploy-role-trust.json) (`<ACCOUNT_ID>` filled in), so only `repo:Hritik456/pagecite:ref:refs/heads/main` can assume it.
   - **Permissions:** add no managed policies. Add an inline policy `pagecite-ssm-deploy` from [deploy/iam/deploy-role-policy.json](../../deploy/iam/deploy-role-policy.json), with `<ACCOUNT_ID>` and `<INSTANCE_ID>` filled in. It can run `AWS-RunShellScript` on this one instance and read the results, nothing else.
   - **Role name:** `pagecite-github-deploy`. Copy its ARN.
3. GitHub → repo → Settings → Secrets and variables → Actions → **Variables** tab. These are variables, not secrets: neither is a credential.
   - `AWS_DEPLOY_ROLE_ARN` = `arn:aws:iam::<ACCOUNT_ID>:role/pagecite-github-deploy`
   - `EC2_INSTANCE_ID` = `<INSTANCE_ID>`

## 11. First deploy

1. Merge the P5B pull request. CI runs, then **Deploy** builds 4 images and pushes them to GHCR.
2. The first Deploy run **fails at the image pull**, because new GHCR packages are private. Make them public once: GitHub → your profile → Packages:
   - `pagecite-api`, `pagecite-worker`, `pagecite-caddy` and `pagecite-migrate`
   - for each: Package settings → Change visibility → **Public**
3. Actions → Deploy → **Re-run failed jobs**. The images already exist, so nothing is rebuilt.
4. Check that <https://pagecite.duckdns.org> loads with a padlock, and that `https://pagecite.duckdns.org/api/health` shows the merge commit's sha. The page shows the full console until step 12.

## 12. Demo tenant + ask-only key (Session Manager)

```sh
sudo /opt/pagecite/pc exec api flask --app apps.api seed-tenant --name demo-rbi --max-file-mb 25 --max-batch-files 60
#   -> tenant_id=1, api_key=pc_...   Put the pc_ key in your password manager, never on the server or in the repo.
sudo /opt/pagecite/pc exec api flask --app apps.api create-ask-key --tenant 1
#   -> ask_key=pca_...  Public by design: the browser sends it. It can only read and ask.
sudo nano /opt/pagecite/.env           # DEMO_ASK_KEY=pca_...
sudo /opt/pagecite/pc up -d caddy      # Caddy re-reads it; /config.json now serves the demo key
```

## 13. Ingest the 60 circulars, watching memory

1. Session Manager tab 1: `sudo /opt/pagecite/sample-memory.sh`. It prints a line every 5 s.
2. On your laptop (PowerShell, repo root, the PDFs in `corpus/rbi-pdfs/`):

   ```powershell
   $env:PAGECITE_ADMIN_KEY = "<the demo-rbi pc_ key>"
   uv run python scripts/prod_ingest.py
   Remove-Item Env:PAGECITE_ADMIN_KEY
   ```
3. When it reports 60/60 INDEXED: Ctrl-C the sampler, then run `sudo /opt/pagecite/oom-check.sh`. The exit code should be 0. Paste both outputs (and `sudo tail -n 200 /var/log/pagecite-mem.log`) back to Claude for `docs/results.md`.

## 14. Reboot test

`sudo reboot`. Wait about 2 minutes, then check that <https://pagecite.duckdns.org> answers a demo question again with nothing done by hand.
