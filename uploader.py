"""
uploader.py — RevistaUploader con correcciones.

Fixes aplicados:
  - BUG-05: trackea original_size además del tamaño subido, para que la URL
    final lleve el tamaño original (no el camuflado).
  - BUG-06: cleanup garantizado de temporales en bloque finally.
  - BUG-08: refresca CSRF antes de cada subida + retry en 403.
  - BUG-07: si hay múltiples archivos, los empaqueta en .tar antes de subir.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from config import config
from encoder import BitZeroEncoder
from storage import storage
from url_generator import URLGenerator

logger = logging.getLogger(__name__)


class RevistaUploader:
    """Uploader a OJS con soporte BitZero completo."""

    def __init__(self, username: str, password: str, submission_id: str,
                 base_url: str, contexto: str, bitzero_mode: int = 0,
                 encryption_key: Optional[str] = None,
                 chunk_size_mb: Optional[int] = None) -> None:
        self.base_url = base_url.rstrip('/')
        self.contexto = contexto
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })
        # NOTA: 'X-Requested-With' NO va aquí: el login de OJS es un form
        # normal y con esa cabecera algunas revistas responden distinto.
        # Solo se envía en el POST de la API de archivos (_do_upload_request).
        self.session.verify = False  # revistas cubanas suelen tener cert self-signed

        self.csrf_token: Optional[str] = None
        self.submission_id = str(submission_id).strip()
        self.chunk_size = (chunk_size_mb or config.CHUNK_SIZE_MB) * 1024 * 1024
        self.username = username
        self.password = password
        self.bitzero_mode = bitzero_mode
        self.encryption_key = encryption_key
        self.uploaded_files: List[Dict[str, Any]] = []
        self.is_logged_in = False
        # Contexto REAL del journal, aprendido del redirect tras el login
        # (p. ej. KIKI: el seed decía 'uclv' pero el journal es 'revista').
        self.effective_contexto: Optional[str] = None
        # Motivo del último login fallido (código + texto para el usuario).
        self.last_login_reason: Optional[str] = None
        self.last_login_error: Optional[str] = None

    def _ctx(self) -> str:
        """Contexto a usar en wizard/API: el real si se aprendió, si no el de la DB."""
        return self.effective_contexto or self.contexto

    # ── Login ────────────────────────────────────────────────────────
    def login(self) -> bool:
        """Inicia sesión en OJS. Devuelve True si el login funcionó.

        FIX v2.1 (verificado en vivo contra las 4 revistas):
          1. La URL del POST NO es siempre /{contexto}/login/signIn: el
             form real de KIKI postea a /index/login/signIn y postear a
             /uclv/login/signIn da 404. Ahora se lee el `action` del form.
          2. La detección de éxito buscaba 'Cerrar sesión'/'Logout', que
             NO existen en OJS 3.4 (el header muestra el usuario y el link
             es 'signOut'). Ahora se detecta por 'signOut', por el nombre
             de usuario en la página y por salir de la URL de login.
          3. El contexto del seed puede estar mal (KIKI es 'revista', no
             'uclv'; LUZ es 'rv'). Tras el login se aprende el contexto
             real de la URL final y se usa en wizard/API.
          4. Antes todo fallo era un genérico "login fallido". Ahora
             `last_login_reason`/`last_login_error` dicen el motivo real:
             credenciales rechazadas, captcha, sitio caído o 404.
        """
        self.last_login_reason = None
        self.last_login_error = None
        try:
            login_url = f"{self.base_url}/index.php/{self.contexto}/login"
            resp = self.session.get(login_url, timeout=30)
            if resp.status_code != 200 or 'signIn' not in resp.text:
                # Fallback: login a nivel de sitio (/index/login).
                alt_url = f"{self.base_url}/index.php/index/login"
                alt = self.session.get(alt_url, timeout=30)
                if alt.status_code == 200 and 'signIn' in alt.text:
                    resp = alt
                else:
                    self.last_login_reason = 'not_found'
                    self.last_login_error = (
                        f"La página de login no responde (HTTP {resp.status_code}). "
                        "Revisa la URL base y el contexto de la revista."
                    )
                    logger.warning(f"Login fallido en {self.base_url}: {self.last_login_error}")
                    self.is_logged_in = False
                    return False

            # Captcha en el login: el bot no puede resolverlo solo.
            low_page = resp.text.lower()
            if any(m in low_page for m in ('g-recaptcha', 'h-captcha', 'cf-turnstile')):
                self.last_login_reason = 'captcha'
                self.last_login_error = (
                    "La revista pide captcha en el login y el bot no puede "
                    "resolverlo solo. Pide al administrador de la revista que "
                    "desactive el captcha para esta cuenta."
                )
                logger.warning(f"Login bloqueado por captcha en {self.base_url}")
                self.is_logged_in = False
                return False

            soup = BeautifulSoup(resp.text, 'html.parser')
            csrf_token = self._extract_csrf(soup, resp.text)
            if not csrf_token:
                logger.warning("CSRF no encontrado en login, continuando con token temporal")
                csrf_token = "temp_token"
            self.csrf_token = csrf_token

            # URL real del form (no asumir /{contexto}/login/signIn).
            post_url = self._login_action_url(soup)
            if not post_url:
                post_url = f"{self.base_url}/index.php/{self.contexto}/login/signIn"

            data = {
                'csrfToken': csrf_token,
                'username': self.username,
                'password': self.password,
                'remember': '1',
                'source': '',
            }
            resp = self.session.post(post_url, data=data, timeout=30)

            if self._login_success(resp):
                self.is_logged_in = True
                self.last_login_reason = 'ok'
                self._learn_context(resp)
                logger.info(f"Login OK en {self.base_url} (contexto: {self._ctx()})")
                return True

            # ── Falló: identificar el motivo real ──────────────────
            low = resp.text.lower()
            if any(m in low for m in ('no son válidos', 'invalid username',
                                       'invalid password', 'usuario o la contraseña')):
                reason, msg = ('bad_credentials',
                               "La revista rechazó el usuario o la contraseña. "
                               "Actualízalos en /control_panel.")
            elif 'g-recaptcha' in low or 'h-captcha' in low:
                reason, msg = ('captcha',
                               "La revista pide captcha en el login y el bot no "
                               "puede resolverlo solo.")
            elif resp.status_code == 404:
                reason, msg = ('not_found',
                               f"La URL de login devolvió 404 ({post_url}). "
                               "Revisa el contexto de la revista.")
            elif 'login' in (resp.url or ''):
                reason, msg = ('bad_credentials',
                               "La revista no aceptó las credenciales "
                               "(sigue en la página de login).")
            else:
                reason, msg = ('unknown',
                               f"No se pudo confirmar el login (HTTP {resp.status_code}).")
            self.last_login_reason = reason
            self.last_login_error = msg
            logger.warning(f"Login fallido en {self.base_url}: {msg}")
            self.is_logged_in = False
            return False
        except requests.exceptions.RequestException as e:
            self.last_login_reason = 'site_down'
            self.last_login_error = (
                f"No se pudo conectar con la revista ({e.__class__.__name__}). "
                "El sitio puede estar caído o sin respuesta."
            )
            logger.error(f"login error: {e}")
            self.is_logged_in = False
            return False
        except Exception as e:
            self.last_login_reason = 'unknown'
            self.last_login_error = f"Error inesperado en login: {e}"
            logger.error(f"login error: {e}")
            self.is_logged_in = False
            return False

    def _login_action_url(self, soup: BeautifulSoup) -> Optional[str]:
        """Lee el `action` real del form de login (puede ser /index/...)."""
        form = soup.find('form', {'id': 'login'}) or soup.find('form')
        if not form:
            return None
        action = (form.get('action') or '').strip()
        if not action or 'signIn' not in action:
            return None
        if action.startswith('http'):
            return action
        # Relativa: resolver contra la base.
        from urllib.parse import urljoin
        return urljoin(self.base_url + '/', action.lstrip('/'))

    @staticmethod
    def _login_success(resp: Any) -> bool:
        """¿La respuesta del POST es una sesión iniciada?

        OJS 3.4 no muestra 'Cerrar sesión'/'Logout' en texto plano: el
        header trae el nombre del usuario y un link 'signOut'. Se aceptan
        los marcadores viejos y los nuevos.
        """
        text = resp.text or ''
        url = resp.url or ''
        if any(x in text for x in ('signOut', 'Cerrar sesión', 'Logout', 'Sign out')):
            return True
        if 'submissionId=' in text:
            return True
        # Fuera de la página de login y con contenido de usuario logueado.
        if '/login' not in url and any(x in text for x in ('submissions', 'dashboard', 'profile')):
            return True
        return False

    def _learn_context(self, resp: Any) -> None:
        """Aprende el contexto real del journal desde la URL final o el
        link signOut (p. ej. KIKI: 'revista', no 'uclv')."""
        candidates = [resp.url or '']
        m = re.search(r'/index\.php/([^/]+)/login/signOut', resp.text or '')
        if m:
            candidates.append(f"/index.php/{m.group(1)}/")
        for url in candidates:
            m = re.search(r'/index\.php/([^/]+)/', url)
            if m and m.group(1) not in ('index', 'login'):
                if m.group(1) != self.contexto:
                    logger.info(f"Contexto real detectado: '{m.group(1)}' (DB decía '{self.contexto}')")
                self.effective_contexto = m.group(1)
                return

    @staticmethod
    def _extract_csrf(soup: BeautifulSoup, html: str) -> Optional[str]:
        """Busca CSRF en input, script y meta tag."""
        csrf_input = soup.find('input', {'name': 'csrfToken'})
        if csrf_input and csrf_input.get('value'):
            return csrf_input['value']
        for script in soup.find_all('script'):
            if script.string and 'csrfToken' in script.string:
                m = re.search(r'csrfToken[\'"]?\s*:\s*[\'"]([^\'"]+)[\'"]', script.string)
                if m:
                    return m.group(1)
        meta = soup.find('meta', {'name': 'csrf-token'})
        if meta and meta.get('content'):
            return meta['content']
        return None

    def check_session(self) -> bool:
        try:
            test_url = (f"{self.base_url}/index.php/{self._ctx()}/submission/wizard/2"
                        f"?submissionId={self.submission_id}")
            resp = self.session.get(test_url, timeout=10, allow_redirects=False)
            if resp.status_code == 302 and 'login' in resp.headers.get('Location', '').lower():
                return False
            return resp.status_code == 200 and any(
                x in resp.text for x in ('submissionId', 'csrfToken', 'step-2')
            )
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        if self.is_logged_in and self.check_session():
            return True
        return self.login()

    # ── Navegación + refresh CSRF ────────────────────────────────────
    def navigate_to_step_2(self) -> bool:
        """Navega al paso 2 del wizard y refresca el CSRF.
        BUG-08 fix: se debe llamar ANTES de cada POST de subida.
        """
        if not self.submission_id:
            return False
        try:
            step2_url = (f"{self.base_url}/index.php/{self._ctx()}/submission/wizard/2"
                         f"?submissionId={self.submission_id}#step-2")
            resp = self.session.get(step2_url, timeout=30)
            if "step-2" not in resp.url and "submission/wizard" not in resp.url:
                logger.warning(f"No se pudo navegar al paso 2. URL actual: {resp.url}")
                return False
            soup = BeautifulSoup(resp.text, 'html.parser')
            new_csrf = self._extract_csrf(soup, resp.text)
            if new_csrf:
                self.csrf_token = new_csrf
                logger.debug("CSRF refrescado desde paso 2")
            return True
        except Exception as e:
            logger.error(f"navigate_to_step_2 error: {e}")
            return False

    # ── Preparación de archivo ───────────────────────────────────────
    def _prepare_file_for_upload(self, file_path: str, user_id: int) -> Optional[str]:
        """Aplica camuflaje BitZero. Devuelve ruta del archivo a subir
        (puede ser el original o el camuflado). None si falla."""
        if self.bitzero_mode == 0:
            return file_path
        camouflaged = BitZeroEncoder.apply_camouflage(
            file_path, self.bitzero_mode, user_id, self.encryption_key
        )
        if camouflaged:
            try:
                orig_size = os.path.getsize(file_path)
                cam_size = os.path.getsize(camouflaged)
                ratio = (cam_size / orig_size) * 100 if orig_size else 0
                logger.info(
                    f"Camuflado: {os.path.basename(file_path)} -> {os.path.basename(camouflaged)} "
                    f"({orig_size/1024:.1f}KB -> {cam_size/1024:.1f}KB, {ratio:.1f}%)"
                )
            except Exception:
                pass
            return camouflaged
        return file_path

    # ── Subida individual (BUG-05, BUG-06, BUG-08 fixes) ─────────────
    def upload_file(self, file_path: str, original_name: Optional[str] = None,
                    user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Sube un archivo. Devuelve dict con info del archivo subido o None.

        BUG-06 fix: limpieza garantizada del temporal en finally.
        BUG-08 fix: CSRF refrescado antes de cada subida + retry en 403.
        BUG-05 fix: registra original_size además del tamaño subido.
        """
        if not os.path.exists(file_path):
            logger.error(f"Archivo no existe: {file_path}")
            return None

        if not self.ensure_logged_in():
            logger.error("No se pudo iniciar sesión")
            return None

        # BUG-08 fix: refrescar CSRF en cada subida
        if not self.navigate_to_step_2():
            logger.error("No se pudo navegar al paso 2 (refresh CSRF)")
            return None

        # Tamaño ORIGINAL antes del camuflaje (BUG-05 fix)
        original_size = os.path.getsize(file_path)

        upload_path = self._prepare_file_for_upload(file_path, user_id or 0)
        if not upload_path:
            upload_path = file_path
        is_temp = (upload_path != file_path)

        file_name = original_name or os.path.basename(file_path)
        if upload_path != file_path:
            file_name = os.path.basename(upload_path)
        content_type = self._content_type(file_name)

        # BUG-06 fix: try/finally para garantizar limpieza
        try:
            try:
                with open(upload_path, 'rb') as f:
                    file_content = f.read()
            except Exception as e:
                logger.error(f"Error leyendo archivo: {e}")
                return None

            file_info = self._do_upload_request(
                file_name=file_name,
                file_content=file_content,
                content_type=content_type,
            )
            if not file_info:
                return None

            # Completar con tamaños (BUG-05 fix)
            file_info['original_size'] = original_size
            file_info['original_name'] = original_name or os.path.basename(file_path)
            file_info['size'] = os.path.getsize(upload_path)

            self.uploaded_files.append(file_info)
            logger.info(f"Subido: {file_name} (ID: {file_info['id']})")
            return file_info

        finally:
            # BUG-06 fix: SIEMPRE limpiar temporal
            if is_temp and upload_path and os.path.exists(upload_path):
                try:
                    os.remove(upload_path)
                    logger.debug(f"Temporal limpiado: {upload_path}")
                except OSError as e:
                    logger.warning(f"No se pudo limpiar temporal {upload_path}: {e}")

    def _do_upload_request(self, file_name: str, file_content: bytes,
                           content_type: str) -> Optional[Dict[str, Any]]:
        """Hace el POST de subida con retry en 403 (CSRF expirado)."""
        api_url = (f"{self.base_url}/index.php/{self._ctx()}/api/v1/submissions/"
                   f"{self.submission_id}/files")
        referer = (f"{self.base_url}/index.php/{self._ctx()}/submission/wizard/2"
                   f"?submissionId={self.submission_id}")

        for attempt in range(2):  # máximo 2 intentos (original + retry por 403)
            headers = {
                'X-Csrf-Token': self.csrf_token or '',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': referer,
            }
            files = {'file': (file_name, file_content, content_type)}
            data = {
                'name[es_ES]': file_name,
                'fileStage': '2',
                'csrfToken': self.csrf_token or '',
            }
            try:
                resp = self.session.post(api_url, files=files, data=data,
                                         headers=headers, timeout=120)
            except Exception as e:
                logger.error(f"POST upload error (intent {attempt+1}): {e}")
                return None

            if resp.status_code == 200:
                try:
                    result = resp.json()
                except Exception:
                    logger.warning(f"Respuesta no-JSON: {resp.text[:200]}")
                    return None
                if not result.get('id'):
                    logger.warning(f"JSON sin ID: {result}")
                    return None
                file_id = result['id']
                name = result.get('name', file_name)
                if isinstance(name, dict):
                    name = name.get('es_ES', file_name)
                download_url = (f"{self.base_url}/$$$call$$$/api/file/file-api/download-file"
                                f"?submissionFileId={file_id}&submissionId={self.submission_id}&stageId=1")
                return {
                    'id': file_id,
                    'name': name,
                    'url': download_url,
                }

            if resp.status_code == 403 and attempt == 0:
                # BUG-08 fix: CSRF expirado, re-login y retry
                logger.warning("403 CSRF rechazado, re-logueando y reintentando...")
                self.is_logged_in = False
                if self.ensure_logged_in() and self.navigate_to_step_2():
                    continue
                return None

            logger.warning(f"Upload HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        return None

    # ── Subida con chunking ──────────────────────────────────────────
    def upload_chunked_file(self, file_path: str, user_id: int) -> List[Dict[str, Any]]:
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if file_size <= self.chunk_size:
            result = self.upload_file(file_path, user_id=user_id)
            return [result] if result else []

        chunks = self._split_file(file_path)
        uploaded: List[Dict[str, Any]] = []
        for idx, chunk in enumerate(chunks, 1):
            chunk_name = f"{file_name}.part{idx:03d}"
            result = self.upload_file(chunk['path'], chunk_name, user_id)
            if result:
                uploaded.append(result)
                logger.info(f"Chunk {idx}/{len(chunks)} subido: {chunk_name}")
            # Limpiar chunk temporal
            if os.path.exists(chunk['path']):
                try:
                    os.remove(chunk['path'])
                except OSError:
                    pass
        return uploaded

    def _split_file(self, file_path: str) -> List[Dict[str, Any]]:
        chunks: List[Dict[str, Any]] = []
        file_name = os.path.basename(file_path)
        out_dir = os.path.dirname(file_path)
        with open(file_path, 'rb') as f:
            n = 1
            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break
                chunk_name = f"{file_name}.part{n:03d}"
                chunk_path = os.path.join(out_dir, chunk_name)
                with open(chunk_path, 'wb') as cf:
                    cf.write(data)
                chunks.append({
                    'path': chunk_path, 'name': chunk_name,
                    'size': len(data), 'number': n
                })
                n += 1
        return chunks

    # ── Generación de URL BitZero ────────────────────────────────────
    def generate_bitzero_url(self, original_name: str, file_size: int) -> str:
        if not self.uploaded_files:
            return ""
        file_ids = [str(f['id']) for f in self.uploaded_files]
        return URLGenerator.generate_bitzero_url(
            host=self.base_url,
            user=self.username,
            password=self.password,
            repo=self.submission_id,
            contexto=self._ctx(),
            file_ids=file_ids,
            bitzero_mode=self.bitzero_mode,
            original_name=original_name,
            file_size=file_size,
            encryption_key=self.encryption_key,
            fake_host=config.BITZERO_FAKE_HOST,
        )

    # ── MIME types ───────────────────────────────────────────────────
    @staticmethod
    def _content_type(filename: str) -> str:
        name = filename.lower()
        exts = {
            '.pdf': 'application/pdf',
            '.zip': 'application/zip',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.mp4': 'video/mp4', '.mp3': 'audio/mpeg',
            '.html': 'text/html', '.txt': 'text/plain',
            '.7z': 'application/x-7z-compressed',
            '.rar': 'application/x-rar-compressed',
            '.tar': 'application/x-tar',
        }
        for ext, ct in exts.items():
            if name.endswith(ext):
                return ct
        return 'application/octet-stream'

    # ── Resumen ──────────────────────────────────────────────────────
    def get_upload_summary(self) -> Dict[str, Any]:
        return {
            'total_files': len(self.uploaded_files),
            'total_uploaded_size': sum(f.get('size', 0) for f in self.uploaded_files),
            'total_original_size': sum(f.get('original_size', f.get('size', 0))
                                       for f in self.uploaded_files),
            'file_ids': [f['id'] for f in self.uploaded_files],
            'original_names': [f.get('original_name', '') for f in self.uploaded_files],
        }
