# Jangil Tickets V2.1 — staging public

Cette version est préparée pour une première mise en ligne sécurisée avant activation des paiements réels.

## Sécurité de staging
- paiement démo désactivé par défaut en `APP_ENV=production`
- dashboard et scanner protégés par `ADMIN_KEY`
- endpoint de santé `/api/health`
- en-têtes de sécurité HTTP
- base SQLite configurable via `JANGIL_DB`

## Railway (recommandé pour la phase staging)
1. Mettre le dossier dans un dépôt GitHub.
2. Créer un projet Railway depuis ce dépôt.
3. Ajouter un Volume monté sur `/data`.
4. Ajouter les variables :
   - `APP_ENV=production`
   - `JANGIL_DB=/data/jangil.db`
   - `DEMO_PAYMENT=false`
   - `ADMIN_KEY=<secret-long-et-aléatoire>`
5. Railway utilisera le `Dockerfile` et le `railway.json`.
6. Dans Networking, générer un domaine public.

## Render (alternative)
Le fichier `render.yaml` prépare un Web Service Docker + disque persistant `/var/data`.

## Local
```bash
pip3 install -r requirements.txt
python3 server.py
```
Ouvrir http://127.0.0.1:8080

## Important avant les ventes
Cette V2.1 reste une version de staging. La prochaine étape sera de migrer la base vers PostgreSQL et d'intégrer le gateway de paiement marchand avant d'accepter de vrais paiements.
