# Desplegar en Railway

1. Sube esta carpeta a un repo **PRIVADO** de GitHub (ver comandos abajo).
2. Railway → New Project → Deploy from GitHub repo → elige el repo. Detecta el Dockerfile solo.
3. Servicio → Settings → Volumes → Add Volume, mount path `/data` (imprescindible: guarda DB, sesión y archivos).
4. No hace falta configurar variables: las credenciales ya están en el código.
5. Una sola réplica.

## Subir a GitHub
    cd bot
    git init
    git add .
    git commit -m "Bot listo para Railway"
    git branch -M main
    git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
    git push -u origin main
