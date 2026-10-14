# Parcel Trail: Courier Tracking System with Flask and DevOps CI/CD

A courier booking and tracking web application, built with Flask and SQLite and delivered through a
Jenkins pipeline that builds a Docker image, tests it, and deploys it with Ansible.

Customers book a parcel and get a waybill with a tracking number. Anyone with that number can follow the
parcel's scan history. Staff sign in to a console and add a scan (status, location, note) each time the
parcel moves.

## Features

| Area | What it does |
|------|--------------|
| Booking | Sender and receiver details, 3 service levels, live price estimate, printable waybill with barcode |
| Tracking | Search by `PT0000000000` style number, 5-step progress bar, newest-first scan history, masked receiver phone |
| Staff console | Login, status tally, search and filter, per-parcel scan form, delivered parcels are locked |
| JSON API | `GET /api/track/<tracking_id>` for apps and integrations |
| Operations | `GET /health` endpoint, Docker health check, persistent Docker volume |
| Security | CSRF token on every POST, constant-time credential check, parameterised SQL, open-redirect guard, non-root container |

### Pricing and delivery promise

| Service | Base fee | Per kg | Delivery promise |
|---------|---------:|-------:|------------------|
| Standard | Rs 40 | Rs 20 | 5 days |
| Express | Rs 80 | Rs 35 | 2 days |
| Overnight | Rs 150 | Rs 55 | 1 day |

Price = base fee + per-kg rate x weight. Example: 2 kg Express = 80 + 35 x 2 = **Rs 150**.

### Status flow

`Booked` -> `Picked Up` -> `In Transit` -> `At Hub` -> `Out for Delivery` -> `Delivered`

`Exception` can be added at any point (for example, receiver not available). The progress bar then keeps
the last healthy step and the tracking page shows a delay notice. `Delivered` closes the parcel.

## Project structure

```
CourierTracking/
├── app.py                # Flask app factory, routes, validation, SQLite access
├── requirements.txt      # Flask, gunicorn, pytest
├── Dockerfile            # python:3.11-slim, non-root user, health check, gunicorn
├── .dockerignore
├── Jenkinsfile           # Checkout, Build, Test, Deploy (Ansible)
├── courier-deploy.yml    # Ansible playbook: volume, container, health wait
├── inventory.ini         # Ansible inventory (localhost)
├── test_app.py           # 17 pytest tests, isolated temp database per test
├── data/                 # SQLite database lives here (Docker volume in production)
├── templates/            # Jinja2 pages and macros (barcode, timeline, status chip)
└── static/               # style.css and app.js
```

## Database design

```
shipments (id, tracking_id UNIQUE, sender_name, sender_phone, receiver_name, receiver_phone,
           origin, destination, weight_kg, service, contents, price, status, eta,
           created_at, delivered_at)
events    (id, shipment_id -> shipments.id, status, location, note, created_at)
```

One shipment has many events. `shipments.status` always equals the status of the newest event, which
keeps the dashboard queries fast while `events` keeps the full audit trail.

## Run locally

```bash
cd CourierTracking
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. Staff sign-in is at `/staff/login`.

Default staff login (development only): **staff / courier123**.
Override with environment variables `STAFF_USERNAME`, `STAFF_PASSWORD`, `SECRET_KEY`.

## Run the tests

```bash
python -m pytest -q
```

The suite covers pricing and ETA maths, booking validation, CSRF rejection, tracking (case-insensitive,
phone masking, unknown numbers), the JSON API, staff login and open-redirect protection, status updates,
the locked "Delivered" state, the Exception progress rule, and console search and filter.

## Docker

```bash
docker build -t parcel-trail .
docker volume create parcel_trail_data
docker run -d --name parcel-trail-app -p 5003:5000 \
  -v parcel_trail_data:/app/data \
  -e STAFF_PASSWORD=choose-a-strong-one -e SECRET_KEY=$(openssl rand -hex 16) \
  parcel-trail
```

Open http://localhost:5003. Because the database sits on the named volume, bookings survive
`docker rm` and redeployments. Check it with `docker volume inspect parcel_trail_data`.

## Ansible deployment

```bash
pip install ansible
ansible-galaxy collection install community.docker
ansible-playbook -i inventory.ini courier-deploy.yml -e image_tag=latest
```

The playbook creates the volume, replaces the container with the new image, then calls `/health` up to
12 times. If the app never becomes healthy, the playbook fails, and so does the Jenkins build.

Pass real secrets with environment variables (`STAFF_PASSWORD`, `SECRET_KEY`) or Ansible Vault, never
by committing them.

## Jenkins pipeline

1. Create a **Pipeline** job, choose *Pipeline script from SCM*, and point it at your Git repository.
2. The agent needs `docker` and `ansible-playbook` on its PATH (plus the `community.docker` collection).
3. Stages:

| Stage | Action | Fails the build when |
|-------|--------|----------------------|
| Checkout | `checkout scm` | repository is unreachable |
| Build image | `docker build` tagged with the build number and `latest` | Dockerfile or dependency error |
| Test | `docker run --rm <image> python -m pytest -q` | any test fails |
| Deploy with Ansible | `ansible-playbook courier-deploy.yml -e image_tag=<build>` | Ansible errors or `/health` never returns 200 |

Add a GitHub webhook (or *Poll SCM*) so every push triggers the pipeline.

## Push to GitHub

```bash
git init
git add .
git commit -m "Parcel Trail courier tracking system"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo>.git
git push -u origin main
```

## API example

```bash
curl http://localhost:5003/api/track/PT1234567890
```

```json
{"tracking_id": "PT1234567890", "status": "In Transit", "origin": "Coimbatore",
 "destination": "Chennai", "service": "Express", "eta": "2026-10-15",
 "events": [{"status": "In Transit", "location": "Salem Hub", "note": "", "time": "2026-10-13 18:40"}]}
```

## Viva questions and answers

**1. Why is the app built with an application factory (`create_app`)?**
Tests can create a fresh app with a temporary database and different config each time, with no shared state.

**2. Why two tables instead of storing the status on one row?**
The status changes many times. The `events` table keeps the whole history (the tracking timeline), and
`shipments.status` stores the latest value so lists and filters stay a single simple query.

**3. How do you stop a form from being submitted by another website?**
Every session gets a random CSRF token, every form includes it, and a `before_request` hook rejects any
POST whose token does not match (compared with `hmac.compare_digest`).

**4. How is SQL injection prevented?**
All queries use `?` placeholders, including the search `LIKE` filter, so user input is never concatenated into SQL.

**5. Why does Jenkins run the tests inside the Docker image?**
The tests then run with exactly the Python version and dependencies that will be deployed. A green test run
means the image itself works.

**6. What does the Ansible playbook give you over `docker run`?**
It is repeatable and declarative: the volume, port mapping, restart policy and environment are described
once in code, applied the same way every time, and followed by a health check.

**7. Why is a Docker volume used?**
A container's filesystem disappears when it is replaced. The named volume `parcel_trail_data` holds
`/app/data/courier.db`, so parcels persist across deployments.

**8. Why gunicorn with one worker?**
Flask's built-in server is for development only. SQLite allows one writer at a time, so one worker with
several threads is simple and safe for this scale. For more load, move to PostgreSQL and add workers.

**9. Why run the container as a non-root user?**
If the app were compromised, the attacker would not have root inside the container.

**10. How would you scale this beyond a lab project?**
Replace SQLite with PostgreSQL, add rate limiting on login, send SMS or email on status changes,
put the app behind an HTTPS reverse proxy, and store secrets in a vault.

## Known limitations

- Single staff account taken from environment variables (no user table or password hashing).
- No rate limiting on the login form.
- SQLite suits a lab or small deployment; it is not meant for many concurrent writers.
