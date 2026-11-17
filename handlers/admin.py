"""
handlers/admin.py — Comandos administrativos y panel de control.

Incluye:
  - /addadmin <id> [@username]   → Añade un administrador dinámico
  - /listadmins                  → Lista todos los administradores
  - /deladmin <id>               → Elimina un administrador (no seeds)
  - /adduser /removeuser /listusers /quota /control_panel (heredados)
  - handle_admin_input (panel de revistas)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import db
from plans import PLANS, expiry_from_now
from utils import (admin_only, clear_admin_state, get_valid_admin_state,
                   safe_handler, set_admin_state)

logger = logging.getLogger(__name__)

# ── /addrevista: pasos del flujo guiado ─────────────────────────────
# Cada paso: (clave_en_data, pregunta). El último paso es 'confirm'.
ADD_REVISTA_STEPS = [
    ("rev_id", "🆔 Envía el **ID de la revista** (ej: `KIKI_REV`, solo letras, números y guión bajo):"),
    ("nombre", "📝 Envía el **nombre** de la revista (ej: `Revista Cardiología`):"),
    ("base_url", "🌐 Envía la **URL base** (ej: `https://revistaciaf.uclv.edu.cu`):"),
    ("contexto", "📝 Envía el **contexto** (el path del journal en la URL, ej: `revista`):"),
    ("username", "👤 Envía el **usuario** de OJS:"),
    ("password", "🔑 Envía la **contraseña** de OJS:"),
    ("submission_id", "🆔 Envía el **submission ID** (el número del envío):"),
    ("bitzero", "🔐 Envía el **modo BitZero** (0, 1, 2 o 3) o escribe `saltar` (= 0):"),
    ("encryption_key", "🔑 Envía la **clave de encriptación** o escribe `saltar` (sin clave):"),
]

_REV_ID_RE = re.compile(r'^[A-Za-z0-9_]{2,30}$')
_CONTEXTO_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,40}$')


def validate_add_revista_value(key: str, value: str):
    """Valida un valor del flujo /addrevista.

    Devuelve (ok, valor_limpio, mensaje_error). 'saltar' vale para
    bitzero (= 0) y encryption_key (= None).
    """
    v = (value or "").strip()
    if key == "rev_id":
        v = v.upper()
        if not _REV_ID_RE.match(v):
            return False, None, "❌ El ID solo puede tener letras, números y guión bajo (2-30 caracteres). Intenta de nuevo."
        return True, v, ""
    if key == "nombre":
        if not v or len(v) > 80:
            return False, None, "❌ El nombre no puede estar vacío ni pasar de 80 caracteres. Intenta de nuevo."
        return True, v, ""
    if key == "base_url":
        v = v.rstrip("/")
        if not v.startswith(("http://", "https://")):
            return False, None, "❌ La URL debe empezar con http:// o https://. Intenta de nuevo."
        return True, v, ""
    if key == "contexto":
        v = v.lower()
        if not _CONTEXTO_RE.match(v):
            return False, None, "❌ El contexto solo puede tener minúsculas, números, - y _ (ej: `revista`). Intenta de nuevo."
        return True, v, ""
    if key == "username":
        if not v or " " in v:
            return False, None, "❌ El usuario no puede estar vacío ni tener espacios. Intenta de nuevo."
        return True, v, ""
    if key == "password":
        if not v:
            return False, None, "❌ La contraseña no puede estar vacía. Intenta de nuevo."
        return True, v, ""
    if key == "submission_id":
        if not v:
            return False, None, "❌ El submission ID no puede estar vacío. Intenta de nuevo."
        return True, v, ""
    if key == "bitzero":
        if v.lower() == "saltar":
            return True, 0, ""
        try:
            n = int(v)
        except ValueError:
            return False, None, "❌ El modo BitZero debe ser 0, 1, 2 o 3, o `saltar`. Intenta de nuevo."
        if n not in (0, 1, 2, 3):
            return False, None, "❌ El modo BitZero debe ser 0, 1, 2 o 3, o `saltar`. Intenta de nuevo."
        return True, n, ""
    if key == "encryption_key":
        if v.lower() == "saltar":
            return True, None, ""
        return True, v, ""
    return False, None, "❌ Paso no válido."


def register(app) -> None:

    @app.on_message(filters.command("setplan"))
    @admin_only
    @safe_handler
    async def set_plan_handler(client, message):
        """Asigna un plan por 30 días: /setplan <user_id> <basico|pro|ultra>."""
        parts = (message.text or "").split()
        if len(parts) != 3 or parts[2].lower() not in PLANS:
            await message.reply("Uso: `/setplan <user_id> <basico|pro|ultra>`")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("El user_id debe ser numérico.")
            return
        plan_key = parts[2].lower()
        expiry = expiry_from_now(plan_key)
        # FIX v2.1: antes fallaba con "No existe ese usuario" si el usuario
        # no estaba en la tabla users (los admins nunca están, y un usuario
        # nuevo tampoco). Ahora se crea automáticamente y se le asigna el plan.
        existed = await db.get_user(target_id) is not None
        if not existed:
            await db.add_user(target_id, None, message.from_user.id)
        updated = await db.set_user_plan(target_id, plan_key, expiry)
        if not updated:
            await message.reply("❌ No se pudo asignar el plan. Inténtalo de nuevo.")
            return
        created_note = "" if existed else "\n👤 Usuario creado automáticamente en la base de datos."
        await message.reply(
            f"✅ Plan **{PLANS[plan_key]['name']}** asignado a `{target_id}` por 30 días.\n"
            f"Vence: `{expiry}` (UTC).{created_note}")

    # ── /cancel — cancelar el flujo activo (editar campo / agregar revista)
    @app.on_message(filters.command("cancel"))
    @admin_only
    @safe_handler
    async def cancel_handler(client, message):
        clear_admin_state(message.from_user.id)
        await message.reply("❌ Operación cancelada.")

    # ── /addrevista — agregar una revista con flujo guiado ──────────
    @app.on_message(filters.command("addrevista"))
    @admin_only
    @safe_handler
    async def add_revista_handler(client, message):
        user_id = message.from_user.id
        if get_valid_admin_state(user_id):
            await message.reply(
                "⚠️ Ya tienes una operación en curso. Termínala o escribe /cancel primero.")
            return
        set_admin_state(user_id, {"action": "add_revista", "step": 0, "data": {}})
        await message.reply(
            "📚 **Agregar revista**\n\n"
            "Te iré pidiendo los datos uno por uno. Escribe `cancelar` en "
            "cualquier momento para salir.\n\n"
            + ADD_REVISTA_STEPS[0][1]
        )

    # ════════════════════════════════════════════════════════════════
    # COMANDOS DE GESTIÓN DE ADMINISTRADORES
    # ════════════════════════════════════════════════════════════════

    @app.on_message(filters.command("addadmin"))
    @admin_only
    @safe_handler
    async def add_admin_handler(client, message):
        """Añade un nuevo administrador dinámico."""
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                "❌ **Uso:** `/addadmin <user_id> [@username]`\n\n"
                "**Ejemplo:** `/addadmin 123456789 @usuario`"
            )
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return
        target_username = parts[2].lstrip('@') if len(parts) > 2 else None

        # No permitir añadirse a sí mismo como duplicado
        if target_id == message.from_user.id:
            await message.reply("❌ Ya eres administrador.")
            return

        added = await db.add_admin(target_id, target_username, message.from_user.id)
        if added:
            await message.reply(
                f"✅ **Administrador añadido**\n\n"
                f"👤 **ID:** `{target_id}`\n"
                f"📛 **Usuario:** @{target_username or 'N/A'}\n"
                f"👑 **Añadido por:** `{message.from_user.id}`\n"
                f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Este usuario ahora tiene acceso total al bot."
            )
        else:
            await message.reply(f"❌ El usuario `{target_id}` ya es administrador.")

    # ── /listadmins — ver todos los administradores ──────────────────
    @app.on_message(filters.command("listadmins"))
    @admin_only
    @safe_handler
    async def list_admins_handler(client, message):
        """Lista todos los administradores (seeds + dinámicos)."""
        admins = await db.list_admins()
        if not admins:
            await message.reply("📭 No hay administradores registrados.")
            return

        text = f"👑 **Administradores ({len(admins)}):**\n\n"
        for i, a in enumerate(admins, 1):
            seed_badge = " 🌱" if a.get('is_seed', 0) == 1 else ""
            text += (
                f"{i}. **ID:** `{a['user_id']}`{seed_badge}\n"
                f"   **Usuario:** @{a.get('username') or 'N/A'}\n"
                f"   **Añadido por:** `{a.get('added_by', 'N/A')}`\n"
                f"   **Fecha:** {a.get('added_date', 'N/A')}\n\n"
            )
        text += "🌱 = Admin semilla (de .env, no eliminable)\n"
        text += f"\n💡 **Para quitar:** `/deladmin <user_id>`"
        await message.reply(text)

    # ── /deladmin — eliminar administrador ───────────────────────────
    @app.on_message(filters.command("deladmin"))
    @admin_only
    @safe_handler
    async def del_admin_handler(client, message):
        """Elimina un administrador (no seeds)."""
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                "❌ **Uso:** `/deladmin <user_id>`\n\n"
                "⚠️ No se pueden eliminar administradores semilla (de .env)"
            )
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return

        if target_id in config.SEED_ADMIN_IDS:
            await message.reply(
                "❌ **No se puede eliminar un administrador semilla.**\n\n"
                "Los administradores definidos en `.env` (ADMIN_ID) son permanentes."
            )
            return

        if target_id == message.from_user.id:
            await message.reply("❌ No puedes eliminarte a ti mismo.")
            return

        removed = await db.remove_admin(target_id)
        if removed:
            await message.reply(
                f"✅ **Administrador eliminado**\n\n"
                f"👤 **ID:** `{target_id}`\n"
                f"👑 **Eliminado por:** `{message.from_user.id}`\n"
                f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            await message.reply(
                f"❌ El usuario `{target_id}` no es administrador o es semilla."
            )

    # Alias /removeadmin
    @app.on_message(filters.command("removeadmin"))
    @admin_only
    @safe_handler
    async def remove_admin_alias(client, message):
        await del_admin_handler(client, message)

    # ════════════════════════════════════════════════════════════════
    # COMANDOS DE GESTIÓN DE USUARIOS (heredados)
    # ════════════════════════════════════════════════════════════════

    @app.on_message(filters.command("adduser"))
    @admin_only
    @safe_handler
    async def add_user_admin(client, message):
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("❌ **Uso:** `/adduser <user_id> [@username]`")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return
        target_username = parts[2].lstrip('@') if len(parts) > 2 else None

        added = await db.add_user(target_id, target_username, message.from_user.id)
        if added:
            await message.reply(
                f"✅ **Usuario añadido**\n\n"
                f"👤 **ID:** `{target_id}`\n"
                f"📛 **Usuario:** @{target_username or 'N/A'}\n"
                f"📅 **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            await message.reply(f"❌ El usuario `{target_id}` ya está autorizado.")

    @app.on_message(filters.command("add"))
    @admin_only
    @safe_handler
    async def add_user_alias(client, message):
        await add_user_admin(client, message)

    @app.on_message(filters.command("removeuser"))
    @admin_only
    @safe_handler
    async def remove_user_admin(client, message):
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("❌ **Uso:** `/removeuser <user_id>`")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("❌ user_id debe ser un entero.")
            return
        if config.is_admin(target_id):
            await message.reply("❌ No puedes eliminar a un administrador. Usa `/deladmin` primero.")
            return
        removed = await db.remove_user(target_id)
        if removed:
            await message.reply(f"✅ **Usuario `{target_id}` eliminado.**")
        else:
            await message.reply(f"❌ El usuario `{target_id}` no existe.")

    @app.on_message(filters.command("ban"))
    @admin_only
    @safe_handler
    async def ban_user_alias(client, message):
        await remove_user_admin(client, message)

    @app.on_message(filters.command("listusers"))
    @admin_only
    @safe_handler
    async def list_users_admin(client, message):
        users = await db.list_users()
        if not users:
            await message.reply("📭 **No hay usuarios autorizados.**")
            return
        text = f"👥 **Usuarios Autorizados ({len(users)}):**\n\n"
        for i, u in enumerate(users, 1):
            status = "✅" if u.get('active', 1) else "❌"
            admin_badge = " 👑" if config.is_admin(u['user_id']) else ""
            text += (
                f"{i}. **ID:** `{u['user_id']}`{admin_badge}\n"
                f"   **Usuario:** @{u.get('username') or 'N/A'}\n"
                f"   **Estado:** {status}\n"
                f"   **Agregado:** {u.get('added_date', 'N/A')}\n"
                f"   **Quota:** {u.get('quota_mb', config.DEFAULT_USER_QUOTA_MB)} MB\n\n"
            )
        await message.reply(text)

    @app.on_message(filters.command("control_panel"))
    @admin_only
    @safe_handler
    async def control_panel(client, message):
        revistas = await db.list_revistas()
        if not revistas:
            await message.reply("📭 No hay revistas configuradas.")
            return
        keyboard = []
        for r in revistas:
            keyboard.append([InlineKeyboardButton(
                f"📚 {r['nombre']} (Modo {r.get('bitzero_mode', 0)})",
                callback_data=f"cp_edit_{r['rev_id']}"
            )])
        keyboard.append([InlineKeyboardButton("➕ Agregar revista", callback_data="cp_add_revista")])
        keyboard.append([InlineKeyboardButton("❌ Cerrar", callback_data="cp_close")])
        await message.reply(
            "🔧 **Panel de Control de Revistas**\n\n"
            "Selecciona una revista para editar sus parámetros.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    @app.on_message(filters.command("quota"))
    @admin_only
    @safe_handler
    async def quota_handler(client, message):
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("❌ **Uso:** `/quota <user_id> <mb>` (usa -1 para ilimitado)")
            return
        try:
            target_id = int(parts[1])
            new_quota = int(parts[2])
        except ValueError:
            await message.reply("❌ Valores deben ser enteros.")
            return
        if not await db.set_user_quota(target_id, new_quota):
            await message.reply(f"❌ Usuario `{target_id}` no encontrado.")
            return
        await message.reply(f"✅ Quota de `{target_id}` actualizada a {new_quota} MB.")

    # ── Flujo de agregar revista (paso a paso) ──────────────────────
    async def _handle_add_revista_step(client, message, state, text):
        """Avanza un paso del flujo /addrevista. `state` trae step y data."""
        user_id = message.from_user.id
        step = state.get("step", 0)
        data = state.get("data", {})

        # Paso de confirmación
        if step == "confirm":
            if text.upper() == "SI":
                from uploader import RevistaUploader
                ok_upsert = await db.upsert_revista(
                    data["rev_id"], nombre=data["nombre"], base_url=data["base_url"],
                    contexto=data["contexto"], username=data["username"],
                    password=data["password"], submission_id=data["submission_id"],
                    bitzero_mode=data.get("bitzero", 0),
                    encryption_key=data.get("encryption_key"), active=1,
                )
                if not ok_upsert:
                    await message.reply("❌ No se pudo guardar la revista. Inténtalo de nuevo.")
                    clear_admin_state(user_id)
                    return
                from handlers.upload import invalidate_uploader
                invalidate_uploader(data["rev_id"])
                # Probar el login de inmediato y decir el motivo si falla.
                uploader = RevistaUploader(
                    username=data["username"], password=data["password"],
                    submission_id=data["submission_id"], base_url=data["base_url"],
                    contexto=data["contexto"], bitzero_mode=data.get("bitzero", 0),
                    encryption_key=data.get("encryption_key"),
                )
                ok = uploader.login()
                await db.update_revista_login_status(data["rev_id"], ok)
                clear_admin_state(user_id)
                if ok:
                    await message.reply(
                        f"✅ **Revista guardada: {data['nombre']}**\n\n"
                        f"🆔 ID: `{data['rev_id']}`\n"
                        f"🌐 URL: {data['base_url']}\n"
                        f"📝 Contexto: `{uploader._ctx()}`\n"
                        f"🔐 BitZero: modo {data.get('bitzero', 0)}\n\n"
                        "🧪 **Prueba de login: ✅ exitosa.** Ya puedes subir a ella con /up."
                    )
                else:
                    reason = getattr(uploader, 'last_login_error', None) or "Revisa credenciales."
                    await message.reply(
                        f"✅ **Revista guardada: {data['nombre']}**\n\n"
                        f"🆔 ID: `{data['rev_id']}`\n\n"
                        f"⚠️ **Pero el login falló:**\n{reason}\n\n"
                        "La revista quedó guardada; corrige sus credenciales en "
                        "/control_panel cuando puedas."
                    )
                return
            if text.upper() == "NO":
                clear_admin_state(user_id)
                await message.reply("❌ Revista descartada, no se guardó nada.")
                return
            await message.reply("Escribe **SI** para guardar o **NO** para cancelar.")
            return

        # Paso de dato (0..len-1)
        key, _prompt = ADD_REVISTA_STEPS[step]
        ok, cleaned, err = validate_add_revista_value(key, text)
        if not ok:
            await message.reply(err)
            return
        if key == "rev_id" and await db.get_revista(cleaned):
            await message.reply(f"❌ Ya existe una revista con ID `{cleaned}`. Elige otro ID.")
            return
        data[key] = cleaned
        next_step = step + 1
        if next_step < len(ADD_REVISTA_STEPS):
            state["data"] = data
            state["step"] = next_step
            set_admin_state(user_id, state)  # refresca TTL en cada paso
            await message.reply(ADD_REVISTA_STEPS[next_step][1])
            return
        # Último dato recogido → confirmación (SIN mostrar la contraseña).
        state["data"] = data
        state["step"] = "confirm"
        set_admin_state(user_id, state)
        summary = (
            "📋 **Revisa los datos:**\n\n"
            f"🆔 ID: `{data['rev_id']}`\n"
            f"📝 Nombre: {data['nombre']}\n"
            f"🌐 URL: {data['base_url']}\n"
            f"📝 Contexto: `{data['contexto']}`\n"
            f"👤 Usuario: `{data['username']}`\n"
            f"🔑 Contraseña: ✅ configurada\n"
            f"🆔 Submission: `{data['submission_id']}`\n"
            f"🔐 BitZero: modo {data.get('bitzero', 0)}\n"
            f"🔑 Clave: {'✅ configurada' if data.get('encryption_key') else '❌ sin clave'}\n\n"
            "Escribe **SI** para guardar o **NO** para cancelar."
        )
        await message.reply(summary)

    # ── Edición de un campo de revista (panel) ──────────────────────
    async def _handle_edit_field(client, message, state, text):
        user_id = message.from_user.id
        rev_id = state["rev_id"]
        field = state["field"]
        new_value = text.strip()

        revista = await db.get_revista(rev_id)
        if not revista:
            await message.reply("❌ Revista no encontrada.")
            clear_admin_state(user_id)
            return

        # FIX v2.1: el botón del panel manda 'bitzero' pero la columna en
        # DB es 'bitzero_mode'. Antes update_revista_field devolvía False
        # (campo no permitido) y aun así se respondía "✅ actualizado".
        db_field = "bitzero_mode" if field == "bitzero" else field
        if db_field == "bitzero_mode":
            try:
                val = int(new_value)
                if val not in [0, 1, 2, 3]:
                    raise ValueError
            except ValueError:
                await message.reply("❌ El modo BitZero debe ser 0, 1, 2 o 3. Intenta de nuevo.")
                return
            saved = await db.update_revista_field(rev_id, db_field, val)
        elif db_field in ["username", "password", "submission_id", "encryption_key",
                          "nombre", "base_url", "contexto"]:
            saved = await db.update_revista_field(rev_id, db_field, new_value)
        else:
            await message.reply("❌ Campo no válido.")
            clear_admin_state(user_id)
            return

        if not saved:
            await message.reply(f"❌ No se pudo guardar el campo **{field}**. Inténtalo de nuevo.")
            clear_admin_state(user_id)
            return

        # Invalidar uploader cacheado
        from handlers.upload import invalidate_uploader
        invalidate_uploader(rev_id)

        await message.reply(f"✅ Campo **{field}** actualizado correctamente.")
        clear_admin_state(user_id)

        # Re-abrir panel
        await control_panel(client, message)

    # ── handle_admin_input — editor de campos de revistas ────────────
    @app.on_message(filters.text & filters.private)
    @safe_handler
    async def handle_admin_input(client, message):
        user_id = message.from_user.id

        # Re-verificar admin + TTL
        state = get_valid_admin_state(user_id)
        if not state:
            return

        # Ignorar si parece comando (además limpia el estado, así /cancel funciona)
        if message.text.startswith('/'):
            clear_admin_state(user_id)
            return

        text = message.text.strip()
        if text.lower() == "cancelar":
            clear_admin_state(user_id)
            await message.reply("❌ Operación cancelada.")
            return

        if state.get("action") == "edit_field":
            await _handle_edit_field(client, message, state, text)
        elif state.get("action") == "add_revista":
            await _handle_add_revista_step(client, message, state, text)

    # ── /broadcast — enviar mensaje a todos los usuarios ──────────────
    @app.on_message(filters.command("broadcast"))
    @admin_only
    @safe_handler
    async def broadcast_handler(client, message):
        """Envía un mensaje a todos los usuarios autorizados del bot."""
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "❌ **Uso:** `/broadcast <mensaje>`\n\n"
                "**Ejemplo:** `/broadcast Hola a todos, el bot se actualizará mañana.`"
            )
            return

        broadcast_text = parts[1]
        users = await db.list_users()
        if not users:
            await message.reply("📭 **No hay usuarios registrados en la base de datos.**")
            return

        status_msg = await message.reply(
            f"📢 **Enviando mensaje a {len(users)} usuarios...**\n\n"
            f"📝 **Mensaje:**\n{broadcast_text[:200]}{'...' if len(broadcast_text) > 200 else ''}"
        )

        success = 0
        failed = 0
        errors = []

        for user in users:
            user_id = user['user_id']
            try:
                await client.send_message(
                    chat_id=user_id,
                    text=(
                        f"📢 **Mensaje del Administrador**\n\n"
                        f"{broadcast_text}\n\n"
                        f"👨‍💻 {config.DEVELOPER_HANDLE}"
                    ),
                    disable_web_page_preview=True
                )
                success += 1
            except Exception as e:
                failed += 1
                errors.append(f"`{user_id}`: {str(e)[:50]}")
                logger.warning(f"Broadcast falló para {user_id}: {e}")

            # Pequeña pausa para evitar rate limiting
            if (success + failed) % 30 == 0:
                await asyncio.sleep(1)

        # Reporte final
        report = (
            f"✅ **Broadcast completado**\n\n"
            f"📊 **Resultados:**\n"
            f"✅ **Enviados:** {success}\n"
            f"❌ **Fallidos:** {failed}\n"
            f"👥 **Total:** {len(users)}\n"
            f"📝 **Mensaje:**\n{broadcast_text[:300]}"
        )

        if errors and len(errors) <= 5:
            report += f"\n\n⚠️ **Errores:**\n" + "\n".join(errors)
        elif errors:
            report += f"\n\n⚠️ **Errores (primeros 5):**\n" + "\n".join(errors[:5])

        await status_msg.edit_text(report)
