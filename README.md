# Litteraturhuset Dagsoversikt

Et web-basert dashboard som viser dagens (og morgendagens) arrangementer fra VenueOps.

## Funksjoner

- 📅 Toggle mellom i dag og i morgen
- 🔄 Auto-oppdatering hvert minutt
- 🎨 Fargekodet status (rigg pågår, get-in, arrangement aktivt)
- 📱 Mobilresponsivt design
- 🔗 Klikkbare lenker til VenueOps
- 👥 Oversikt over verter

## Installasjon

### Lokalt

```bash
# Klon/kopier filene
cd litteraturhuset-dashboard

# Installer avhengigheter
pip install -r requirements.txt

# Kjør
python app.py
```

Åpne `http://localhost:5000` i nettleseren.

### Produksjon (Linux server)

#### Med systemd

1. Kopier filene til serveren, f.eks. `/opt/litteraturhuset-dashboard/`

2. Installer avhengigheter:
```bash
cd /opt/litteraturhuset-dashboard
pip install -r requirements.txt
```

3. Opprett systemd service (`/etc/systemd/system/litteraturhuset.service`):
```ini
[Unit]
Description=Litteraturhuset Dashboard
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/litteraturhuset-dashboard
ExecStart=/usr/bin/python3 app.py
Restart=always
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
```

4. Aktiver og start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable litteraturhuset
sudo systemctl start litteraturhuset
```

#### Med nginx (anbefalt for produksjon)

Bruk gunicorn som WSGI-server:

```bash
pip install gunicorn
gunicorn -w 2 -b 127.0.0.1:5000 app:app
```

Nginx config (`/etc/nginx/sites-available/litteraturhuset`):
```nginx
server {
    listen 80;
    server_name ditt-domene.no;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Struktur

```
litteraturhuset-dashboard/
├── app.py              # Flask backend
├── templates/
│   └── index.html      # Frontend
├── requirements.txt
└── README.md
```

## Fargekoding

| Farge | Betydning |
|-------|-----------|
| Hvit | Rigg pågår |
| Gul | Get-in pågår |
| Cyan | Arrangement pågår |
| Mørk grå | Venter / Ferdig |

## API Endpoints

- `GET /` - Dashboard HTML
- `GET /api/data/today` - JSON data for i dag
- `GET /api/data/tomorrow` - JSON data for i morgen

## Sikkerhet

⚠️ **Viktig:** API-credentials er hardkodet i `app.py`. For produksjon, flytt disse til miljøvariabler:

```python
CLIENT_ID = os.environ.get('VENUEOPS_CLIENT_ID')
CLIENT_SECRET = os.environ.get('VENUEOPS_CLIENT_SECRET')
```

Og sett dem i systemd-filen eller `.env`:
```bash
export VENUEOPS_CLIENT_ID="..."
export VENUEOPS_CLIENT_SECRET="..."
```
