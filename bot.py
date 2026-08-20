"""DB を正として Discord から Minecraft ハードコア鯖を提供するボット。"""
import asyncio, logging, os, re, secrets, shlex, string
from dataclasses import dataclass
from datetime import timedelta, timezone

import discord
import mysql.connector
import paramiko
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MCID_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
DEFAULT_VERSION_ROWS = (
    ("paper", "1.21.8", 60, "https://fill-data.papermc.io/v1/objects/8de7c52c3b02403503d16fac58003f1efef7dd7a0256786843927fa92ee57f1e/paper-1.21.8-60.jar", True),
    ("paper", "1.21.10", 112, "https://fill-data.papermc.io/v1/objects/d901c205cebd2c14e2d92c5fcbd0ba95add71da9726fc7829d1431a8b80969b6/paper-1.21.10-112.jar", True),
    ("paper", "1.21.10", 113, "https://fill-data.papermc.io/v1/objects/d4f897545310f31e623d9680786b25dd20a9989e139db050d1aacf81ecafd05c/paper-1.21.10-113.jar", True),
    ("paper", "1.21.11", 38, "https://fill-data.papermc.io/v1/objects/7c16d3931f725a575aa6caa3e537d0ccc962e1413644f9bb31f885fc3d6a9a98/paper-1.21.11-38.jar", True),
    ("paper", "1.21.11", 92, "https://fill-data.papermc.io/v1/objects/f3f6bb1f913bd977da65edaec79ec94ced7c7971352d8630eddf782d6af0f03c/paper-1.21.11-92.jar", True),
    ("paper", "26.1.2", 63, "https://fill-data.papermc.io/v1/objects/b51d49a5f62446b7cfc01e6c29e48e0ce6abd35a783134aace1047b839b178ef/paper-26.1.2-63.jar", True),
    ("paper", "26.1.2", 71, "https://fill-data.papermc.io/v1/objects/542288423062864e56969a44c6927b860152cb827c65ff5f841178602bc99e9a/paper-26.1.2-71.jar", True),
    ("paper", "26.2.0", 111, "https://fill-data.papermc.io/v1/objects/3ec81e3ea50cc6090b94aab024491846a202702e8a874308a5d7510f6b3aa012/paper-26.2-111.jar", True),
    *(("vanilla", version, 1, "1", True) for version in ("1.20.1", "1.20.2", "1.20.3", "1.20.4", "1.20.5", "1.20.6", "1.21.2", "1.21.3", "1.21.4", "1.21.5", "1.21.6", "1.21.7", "1.21.8")),
)
class CapacityError(RuntimeError): pass

def env(name):
    value = os.getenv(name, "").strip()
    if not value: raise RuntimeError(f"環境変数 {name} が未設定です")
    return value

@dataclass(frozen=True)
class Config:
    token: str; domain: str; admin_key: str; db: dict; ssh: dict; min_port: int; max_port: int; retention_days: int
    @classmethod
    def load(cls):
        return cls(env("DISCORD_BOT_TOKEN"), env("DOMAIN_NAME"), env("ADMIN_KEY"),
            {"host":env("DB_HOST"),"database":env("DB_NAME"),"port":int(env("DB_PORT")),"user":env("DB_USER"),"password":env("DB_PASS")},
            {"hostname":env("SSH_HOST"),"port":int(env("SSH_PORT")),"username":env("SSH_USER"),"password":os.getenv("SSH_PASS") or None,"key_filename":os.getenv("SSH_KEY_PATH") or None},
            int(env("SV_MIN_PORT")), int(env("SV_MAX_PORT")), int(os.getenv("RESET_RETENTION_DAYS", "30")))

class Store:
    def __init__(self, config): self.config, self.port_lock = config, asyncio.Lock()
    def _query(self, sql, values=(), fetch=False):
        conn = mysql.connector.connect(**self.config.db)
        try:
            cur = conn.cursor(dictionary=True); cur.execute(sql, values)
            result = cur.fetchall() if fetch else None; conn.commit(); return result
        finally: conn.close()
    async def query(self, *args, **kwargs): return await asyncio.to_thread(self._query, *args, **kwargs)
    def _bootstrap(self):
        """既存 discord-mc-admin DB を壊さず必要な拡張だけを追加する。"""
        conn = mysql.connector.connect(**self.config.db)
        try:
            cur = conn.cursor()
            # A dedicated mc_hc_admin database starts empty.  Create the
            # complete application schema before applying incremental upgrades.
            cur.execute("""CREATE TABLE IF NOT EXISTS perm_limits (
                perm_name VARCHAR(50) NOT NULL PRIMARY KEY,
                max_sv INT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cur.execute("INSERT IGNORE INTO perm_limits(perm_name,max_sv) VALUES ('default',1),('premium',3),('admin',999)")
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                dc_user_id VARCHAR(50) NOT NULL PRIMARY KEY,
                dc_user_name VARCHAR(255) NOT NULL,
                perm_name VARCHAR(50) NOT NULL,
                dc_created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
                KEY perm_name (perm_name),
                CONSTRAINT users_ibfk_1 FOREIGN KEY (perm_name) REFERENCES perm_limits(perm_name) ON UPDATE CASCADE ON DELETE RESTRICT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cur.execute("""CREATE TABLE IF NOT EXISTS servers (
                sv_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                dc_user_id VARCHAR(50) NOT NULL,
                sv_name VARCHAR(255) NOT NULL,
                sv_type VARCHAR(50) NOT NULL,
                sv_ver VARCHAR(20) NOT NULL,
                sv_port INT NULL,
                status ENUM('running','stopped','creating','deleting','deleted','error') NOT NULL DEFAULT 'error',
                sv_created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
                last_reset_at DATETIME NULL,
                reset_code CHAR(8) NULL,
                voice_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE KEY unique_port (sv_port),
                UNIQUE KEY unique_reset_code (reset_code),
                KEY idx_servers_owner_name_nonunique (dc_user_id,sv_name),
                CONSTRAINT servers_ibfk_1 FOREIGN KEY (dc_user_id) REFERENCES users(dc_user_id) ON UPDATE CASCADE ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cur.execute("""CREATE TABLE IF NOT EXISTS server_versions (
                sv_ver_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                sv_type VARCHAR(50) NOT NULL,
                sv_ver VARCHAR(20) NOT NULL,
                build_ver INT NOT NULL DEFAULT 1,
                download_url VARCHAR(255) NOT NULL DEFAULT '1',
                is_supported BOOLEAN NOT NULL DEFAULT TRUE,
                UNIQUE KEY sv_type (sv_type,sv_ver,build_ver)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cur.execute("SELECT COUNT(*) FROM server_versions")
            if cur.fetchone()[0] == 0:
                cur.executemany("INSERT INTO server_versions(sv_type,sv_ver,build_ver,download_url,is_supported) VALUES(%s,%s,%s,%s,%s)", DEFAULT_VERSION_ROWS)
                log.info("DB bootstrap: added %s default server versions", len(DEFAULT_VERSION_ROWS))
            cur.execute("SHOW COLUMNS FROM servers LIKE 'voice_enabled'")
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE servers ADD COLUMN voice_enabled BOOLEAN NOT NULL DEFAULT FALSE")
                log.info("DB migration: added servers.voice_enabled")
            cur.execute("SHOW COLUMNS FROM servers LIKE 'last_reset_at'")
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE servers ADD COLUMN last_reset_at DATETIME NULL")
                cur.execute("UPDATE servers SET last_reset_at=UTC_TIMESTAMP() WHERE last_reset_at IS NULL")
                log.info("DB migration: added servers.last_reset_at")
            cur.execute("SHOW COLUMNS FROM servers LIKE 'reset_code'")
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE servers ADD COLUMN reset_code CHAR(8) NULL")
                cur.execute("ALTER TABLE servers ADD UNIQUE KEY unique_reset_code (reset_code)")
                log.info("DB migration: added servers.reset_code")
            # 旧スキーマには (dc_user_id, sv_name) を UNIQUE にしているものがある。
            # deleted 行は履歴として保持するため、その制約では削除後に同名再作成できず、
            # admin の複数作成にも不適切。外部キー用の通常インデックスを先に用意してから外す。
            cur.execute("SHOW INDEX FROM servers")
            indexes = {}
            for row in cur.fetchall():
                indexes.setdefault(row[2], {"unique": row[1] == 0, "columns": []})["columns"].append((row[3], row[4]))
            owner_unique = []
            for index_name, data in indexes.items():
                columns = [column for _, column in sorted(data["columns"])]
                if data["unique"] and index_name != "PRIMARY" and columns in (["dc_user_id"], ["dc_user_id", "sv_name"]):
                    owner_unique.append(index_name)
            if owner_unique:
                existing_owner_index = indexes.get("idx_servers_owner_name")
                if existing_owner_index is None or existing_owner_index["unique"]:
                    cur.execute("ALTER TABLE servers ADD INDEX idx_servers_owner_name_nonunique (dc_user_id, sv_name)")
                for index_name in owner_unique:
                    cur.execute(f"ALTER TABLE servers DROP INDEX `{index_name}`")
                    log.info("DB migration: dropped obsolete unique index %s", index_name)
            cur.execute("""CREATE TABLE IF NOT EXISTS server_events (
                event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
                sv_id INT NULL, dc_user_id VARCHAR(50) NULL,
                event_type VARCHAR(40) NOT NULL, detail TEXT NULL,
                occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_events_server (sv_id), INDEX idx_events_user (dc_user_id),
                CONSTRAINT fk_event_server FOREIGN KEY (sv_id) REFERENCES servers(sv_id) ON UPDATE CASCADE ON DELETE SET NULL,
                CONSTRAINT fk_event_user FOREIGN KEY (dc_user_id) REFERENCES users(dc_user_id) ON UPDATE CASCADE ON DELETE SET NULL
            )""")
            # A server without an assigned port cannot be managed or reached.
            # Older incomplete records are retained as deleted history instead of
            # blocking a user or breaking /status.
            cur.execute("UPDATE servers SET status='deleted' WHERE sv_port IS NULL AND status<>'deleted'")
            if cur.rowcount:
                log.info("DB migration: marked %s unassigned server records as deleted", cur.rowcount)
            conn.commit()
        finally: conn.close()
    async def bootstrap(self): await asyncio.to_thread(self._bootstrap)
    async def ensure_user(self, user):
        await self.query("INSERT INTO users(dc_user_id,dc_user_name,perm_name) VALUES(%s,%s,'default') ON DUPLICATE KEY UPDATE dc_user_name=VALUES(dc_user_name)", (str(user.id), user.name))
    async def permission(self, user_id): return (await self.query("SELECT perm_name FROM users WHERE dc_user_id=%s", (str(user_id),), True))[0]["perm_name"]
    async def event(self, event_type, user_id, sv_id=None, detail=None):
        await self.query("INSERT INTO server_events(sv_id,dc_user_id,event_type,detail) VALUES(%s,%s,%s,%s)", (sv_id, str(user_id) if user_id else None, event_type, detail))
    async def versions(self, server_type):
        return await self.query("SELECT v.sv_ver,v.download_url FROM server_versions v INNER JOIN (SELECT sv_ver,MAX(build_ver) AS newest FROM server_versions WHERE sv_type=%s AND is_supported=TRUE GROUP BY sv_ver) latest ON v.sv_ver=latest.sv_ver AND v.build_ver=latest.newest WHERE v.sv_type=%s AND v.is_supported=TRUE ORDER BY CAST(SUBSTRING_INDEX(v.sv_ver,'.',1) AS UNSIGNED) DESC,CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(v.sv_ver,'.',2),'.',-1) AS UNSIGNED) DESC,CAST(SUBSTRING_INDEX(v.sv_ver,'.',-1) AS UNSIGNED) DESC LIMIT 25", (server_type,server_type), True)
    async def latest(self, server_type):
        rows = await self.versions(server_type)
        if not rows: raise RuntimeError(f"{server_type} の利用可能なバージョンが DB にありません")
        return rows[0]
    async def reserve(self, user_id, name, server_type, version, voice_enabled=False):
        async with self.port_lock:
            perm = await self.permission(user_id)
            if perm == "default":
                active = await self.query("SELECT sv_id FROM servers WHERE dc_user_id=%s AND sv_port IS NOT NULL AND status<>'deleted'", (str(user_id),), True)
                if active:
                    await self.event("create_rejected", user_id, active[0]["sv_id"], "already_has_server")
                    return None
            duplicate = await self.query("SELECT sv_id FROM servers WHERE dc_user_id=%s AND sv_name=%s AND sv_port IS NOT NULL AND status<>'deleted'", (str(user_id), name), True)
            if duplicate:
                await self.event("create_rejected", user_id, duplicate[0]["sv_id"], "duplicate_name")
                raise ValueError("同じ名前のサーバーがすでに存在します")
            used = {r["sv_port"] for r in await self.query("SELECT sv_port FROM servers WHERE status<>'deleted'", fetch=True)}
            port = next((p for p in range(self.config.min_port, self.config.max_port + 1) if p not in used), None)
            if port is None:
                await self.event("create_rejected_capacity", user_id, detail="all 10 hardcore slots are occupied")
                raise CapacityError("現在、ハードコアサーバーの作成枠がすべて使用中です。空きが出るまで作成できません。")
            # コードはパスワードではなく、共有リセット用の識別子として平文保存する。
            alphabet = string.ascii_uppercase + string.digits
            for _ in range(10):
                reset_code = ''.join(secrets.choice(alphabet) for _ in range(8))
                try:
                    await self.query("INSERT INTO servers(dc_user_id,sv_name,sv_type,sv_ver,sv_port,status,last_reset_at,reset_code,voice_enabled) VALUES(%s,%s,%s,%s,%s,'creating',UTC_TIMESTAMP(),%s,%s)", (str(user_id),name,server_type,version,port,reset_code,voice_enabled))
                    break
                except mysql.connector.IntegrityError:
                    continue
            else: raise RuntimeError("リセットコードを発行できませんでした")
            # A deleted server can have the same owner/name. The just-reserved
            # port identifies the new row unambiguously.
            row = (await self.query("SELECT sv_id FROM servers WHERE dc_user_id=%s AND sv_name=%s AND sv_port=%s ORDER BY sv_id DESC LIMIT 1", (str(user_id),name,port), True))[0]
            await self.event("create_requested", user_id, row["sv_id"], f"{server_type} {version}; port={port}")
            return {"id":row["sv_id"],"port":port,"reset_code":reset_code}
    async def create_result(self, server, user_id, ok):
        await self.query("UPDATE servers SET status=%s WHERE sv_id=%s", ("running" if ok else "error",server["id"]))
        await self.event("created" if ok else "create_failed", user_id, server["id"])
    async def get_server(self, user_id, name):
        rows = await self.query("SELECT sv_id,sv_port FROM servers WHERE dc_user_id=%s AND sv_name=%s AND sv_port IS NOT NULL AND status<>'deleted'", (str(user_id),name), True)
        return rows[0] if rows else None
    async def server_for_reset_code(self, reset_code):
        rows = await self.query("SELECT sv_id,sv_port FROM servers WHERE reset_code=%s AND sv_port IS NOT NULL AND status IN ('running','stopped','error')", (reset_code.upper(),), True)
        return rows[0] if rows else None
    async def servers_for_user(self, user_id):
        return await self.query("SELECT sv_id,sv_name,sv_type,sv_ver,sv_port,status,last_reset_at,reset_code FROM servers WHERE dc_user_id=%s AND sv_port IS NOT NULL AND status<>'deleted' ORDER BY sv_id", (str(user_id),), True)
    async def reset_result(self, server, user_id, ok):
        if ok: await self.query("UPDATE servers SET status='running',last_reset_at=UTC_TIMESTAMP() WHERE sv_id=%s", (server["sv_id"],))
        await self.event("reset" if ok else "reset_failed", user_id, server["sv_id"])
    async def expired(self):
        return await self.query("SELECT sv_id,dc_user_id,sv_port FROM servers WHERE sv_port IS NOT NULL AND status IN ('running','stopped','error') AND last_reset_at < UTC_TIMESTAMP() - INTERVAL %s DAY", (self.config.retention_days,), True)
    async def deleted(self, server, ok):
        if ok: await self.query("UPDATE servers SET status='deleted',sv_port=NULL WHERE sv_id=%s", (server["sv_id"],))
        await self.event("expired_deleted" if ok else "expired_delete_failed", None, server["sv_id"], f"owner={server['dc_user_id']}")
    async def manual_deleted(self, server, user_id, ok):
        if ok: await self.query("UPDATE servers SET status='deleted',sv_port=NULL WHERE sv_id=%s", (server["sv_id"],))
        await self.event("deleted" if ok else "delete_failed", user_id, server["sv_id"])

class Remote:
    def __init__(self, config): self.config = config
    def run(self, action, port=None, server_type=None, version=None, url=None, voice_enabled=False, mcid=None):
        if action == "prune-backups":
            args = [action, str(self.config.retention_days)]
        else:
            if action not in {"create","reset","delete","status","op"} or port is None or not self.config.min_port <= port <= self.config.max_port: raise ValueError("不正な管理操作")
            args = [action, str(port)]
            if action == "reset": args.append(str(self.config.retention_days))
            if action == "op":
                if not MCID_RE.fullmatch(mcid or ""): raise ValueError("不正なMinecraft ID")
                args.append(mcid)
        if action == "create":
            if server_type not in {"vanilla","paper"} or not re.fullmatch(r"[0-9.]+", version or "") or not (url or "").startswith("https://"): raise ValueError("不正な作成情報")
            args += [server_type, version, url, "1" if voice_enabled else "0"]
        client = paramiko.SSHClient(); client.load_system_host_keys(); client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(**self.config.ssh, look_for_keys=not bool(self.config.ssh["password"]), allow_agent=False, timeout=15)
            command = "sudo -n /usr/local/sbin/hardcore-pool-admin " + " ".join(shlex.quote(a) for a in args)
            _, out, err = client.exec_command(command, timeout=300); code = out.channel.recv_exit_status()
            detail = (out.read()+err.read()).decode(errors="replace").strip()
            if code: log.error("remote %s failed: %s", action, detail)
            return code == 0
        finally: client.close()

async def provision(bot, interaction, name, server_type, version_data, voice_enabled=False):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try: server = await bot.store.reserve(interaction.user.id, name, server_type, version_data["sv_ver"], voice_enabled)
    except (ValueError, CapacityError) as e: await interaction.followup.send(str(e), ephemeral=True); return
    except Exception: log.exception("reserve failed"); await interaction.followup.send("作成枠を確保できませんでした。", ephemeral=True); return
    if server is None:
        await interaction.followup.send("すでにサーバーを作成済みです。default 権限では 1 人 1 台までです。", ephemeral=True); return
    url = version_data["download_url"] if server_type == "paper" else "https://example.invalid/unused.jar"
    try: ok = await asyncio.to_thread(bot.remote.run, "create", server["port"], server_type, version_data["sv_ver"], url, voice_enabled)
    except Exception: log.exception("create failed"); ok = False
    if not ok:
        # 作成途中にできたサービス・ディレクトリを残すと、次回作成を妨げる。
        try:
            cleaned = await asyncio.to_thread(bot.remote.run, "delete", server["port"])
            await bot.store.event("create_cleanup" if cleaned else "create_cleanup_failed", interaction.user.id, server["id"])
        except Exception:
            log.exception("failed to clean up incomplete server %s", server["port"])
    await bot.store.create_result(server, interaction.user.id, ok)
    if ok: await interaction.followup.send(f"サーバーを作成しました。接続先: `{bot.address_for(server['port'])}`\nリセットコード: `{server['reset_code']}`", ephemeral=True)
    else: await interaction.followup.send("作成に失敗しました。管理者へ連絡してください。", ephemeral=True)

class OwnerView(discord.ui.View):
    def __init__(self, bot, owner, name): super().__init__(timeout=120); self.bot,self.owner,self.name=bot,owner,name
    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner: return True
        await interaction.response.send_message("この選択はコマンドを実行した本人だけが操作できます。", ephemeral=True); return False

class EulaView(OwnerView):
    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def agree(self, interaction, _):
        await interaction.response.send_message("近接 VC を利用しますか？", view=ProximityView(self.bot,self.owner,self.name), ephemeral=True); self.stop()
    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction, _): await interaction.response.send_message("作成を中止しました。", ephemeral=True); self.stop()

class ProximityView(OwnerView):
    @discord.ui.button(label="あり（Paper）", style=discord.ButtonStyle.primary)
    async def paper(self, interaction, _):
        try: versions = await self.bot.store.versions("paper")
        except Exception: log.exception("versions failed"); await interaction.response.send_message("バージョン一覧を取得できませんでした。", ephemeral=True); return
        if not versions: await interaction.response.send_message("利用可能な Paper バージョンがありません。", ephemeral=True); return
        await interaction.response.send_message("Paper のバージョンを選択してください。", view=VersionView(self.bot,self.owner,self.name,versions,True), ephemeral=True); self.stop()
    @discord.ui.button(label="なし（最新 Paper）", style=discord.ButtonStyle.secondary)
    async def vanilla(self, interaction, _):
        try: version = await self.bot.store.latest("paper")
        except Exception: log.exception("latest failed"); await interaction.response.send_message("最新バージョンを取得できませんでした。", ephemeral=True); return
        self.stop(); await provision(self.bot, interaction, self.name, "paper", version, False)

class VersionView(OwnerView):
    def __init__(self, bot, owner, name, versions, voice_enabled):
        super().__init__(bot,owner,name); self.versions={v["sv_ver"]:v for v in versions}; self.voice_enabled=voice_enabled
        self.add_item(VersionSelect(self.versions))
    async def chosen(self, interaction, version): self.stop(); await provision(self.bot, interaction, self.name, "paper", self.versions[version], self.voice_enabled)
class VersionSelect(discord.ui.Select):
    def __init__(self, versions): super().__init__(placeholder="Paper バージョンを選択", options=[discord.SelectOption(label=v) for v in versions])
    async def callback(self, interaction): await self.view.chosen(interaction, self.values[0])

class ResetConfirmView(OwnerView):
    def __init__(self, bot, owner, server): super().__init__(bot,owner,""); self.server=server
    @discord.ui.button(label="はい", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try: ok=await asyncio.to_thread(self.bot.remote.run,"reset",self.server["sv_port"])
        except Exception: log.exception("reset failed"); ok=False
        await self.bot.store.reset_result(self.server,interaction.user.id,ok)
        await interaction.followup.send(f"ワールドをリセットしました。{self.bot.config.retention_days} 日後を目安に自動削除されます。" if ok else "リセットに失敗しました。",ephemeral=True)
        self.stop()
    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _): await interaction.response.send_message("リセットを中止しました。",ephemeral=True); self.stop()

class ResetSelectView(OwnerView):
    def __init__(self, bot, owner, servers):
        super().__init__(bot,owner,""); self.servers={str(s["sv_id"]):s for s in servers}
        self.add_item(ResetSelect(self.servers))
    async def chosen(self, interaction, server):
        await interaction.response.send_message("このサーバーのワールドをリセットしますか？",view=ResetConfirmView(self.bot,self.owner,server),ephemeral=True); self.stop()
class ResetSelect(discord.ui.Select):
    def __init__(self, servers):
        super().__init__(placeholder="リセットするサーバーを選択",options=[discord.SelectOption(label=s["sv_name"],value=str(s["sv_id"])) for s in servers.values()])
    async def callback(self, interaction): await self.view.chosen(interaction,self.view.servers[self.values[0]])

class DeleteConfirmView(OwnerView):
    def __init__(self, bot, owner, server): super().__init__(bot,owner,""); self.server=server
    @discord.ui.button(label="はい", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _):
        await interaction.response.defer(ephemeral=True,thinking=True)
        try: ok=await asyncio.to_thread(self.bot.remote.run,"delete",self.server["sv_port"])
        except Exception: log.exception("delete failed"); ok=False
        await self.bot.store.manual_deleted(self.server,interaction.user.id,ok)
        await interaction.followup.send("サーバーを削除しました。新しく作成できます。" if ok else "サーバー削除に失敗しました。",ephemeral=True)
        self.stop()
    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _): await interaction.response.send_message("削除を中止しました。",ephemeral=True); self.stop()

class DeleteSelectView(OwnerView):
    def __init__(self, bot, owner, servers):
        super().__init__(bot,owner,""); self.servers={str(s["sv_id"]):s for s in servers}; self.add_item(DeleteSelect(self.servers))
    async def chosen(self, interaction, server):
        await interaction.response.send_message("このサーバーを完全に削除しますか？",view=DeleteConfirmView(self.bot,self.owner,server),ephemeral=True); self.stop()
class DeleteSelect(discord.ui.Select):
    def __init__(self, servers): super().__init__(placeholder="削除するサーバーを選択",options=[discord.SelectOption(label=s["sv_name"],value=str(s["sv_id"])) for s in servers.values()])
    async def callback(self, interaction): await self.view.chosen(interaction,self.view.servers[self.values[0]])

class OpSelectView(OwnerView):
    def __init__(self, bot, owner, servers):
        super().__init__(bot,owner,""); self.servers={str(s["sv_id"]):s for s in servers}; self.add_item(OpSelect(self.servers))
    async def chosen(self, interaction, server):
        await interaction.response.send_modal(OpModal(self.bot, self.owner, server)); self.stop()

class OpSelect(discord.ui.Select):
    def __init__(self, servers): super().__init__(placeholder="OP権限を付与するサーバーを選択",options=[discord.SelectOption(label=s["sv_name"],value=str(s["sv_id"])) for s in servers.values()])
    async def callback(self, interaction): await self.view.chosen(interaction,self.view.servers[self.values[0]])

class OpModal(discord.ui.Modal, title="Minecraft IDを入力"):
    mcid = discord.ui.TextInput(label="Minecraftユーザー名", placeholder="例: Steve", min_length=3, max_length=16)
    def __init__(self, bot, owner, server):
        super().__init__(timeout=120); self.bot,self.owner,self.server=bot,owner,server
    async def on_submit(self, interaction):
        name = self.mcid.value
        if not MCID_RE.fullmatch(name):
            await self.bot.store.event("op_rejected", interaction.user.id, self.server["sv_id"], "invalid_mcid")
            await interaction.response.send_message("Minecraft IDは英数字と `_` の3〜16文字にしてください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try: ok = await asyncio.to_thread(self.bot.remote.run, "op", self.server["sv_port"], mcid=name)
        except Exception: log.exception("op failed"); ok = False
        await self.bot.store.event("op_granted" if ok else "op_failed", interaction.user.id, self.server["sv_id"], f"mcid={name}")
        await interaction.followup.send(f"`{name}` にOP権限を付与しました。" if ok else "OP権限の付与に失敗しました。サーバーが起動中か確認してください。", ephemeral=True)

class Bot(commands.Bot):
    def __init__(self, config): super().__init__(command_prefix="!",intents=discord.Intents.none()); self.config=config; self.store=Store(config); self.remote=Remote(config)
    async def setup_hook(self):
        await self.store.bootstrap()
        await self.add_cog(ControlCog(self)); self.cleanup.start(); await self.tree.sync()
    def address_for(self, port):
        # 25401 -> hc01, 25410 -> hc10。BungeeCord の転送設定は VM 側で管理する。
        return f"hc{port - self.config.min_port + 1:02d}.{self.config.domain}"
    async def close(self): self.cleanup.cancel(); await super().close()
    @tasks.loop(hours=6)
    async def cleanup(self):
        try: expired = await self.store.expired()
        except Exception:
            log.exception("could not query expired servers")
            return
        for server in expired:
            try: ok = await asyncio.to_thread(self.remote.run,"delete",server["sv_port"])
            except Exception: log.exception("automatic deletion failed"); ok=False
            await self.store.deleted(server,ok)
        try:
            await asyncio.to_thread(self.remote.run, "prune-backups")
        except Exception:
            log.exception("could not prune expired world backups")
    @cleanup.before_loop
    async def before_cleanup(self): await self.wait_until_ready()

class ControlCog(commands.Cog):
    def __init__(self, bot): self.bot=bot
    async def user(self, interaction): await self.bot.store.ensure_user(interaction.user)
    @app_commands.command(description="管理者権限を有効化します")
    async def admin(self, interaction, key: str):
        await self.user(interaction)
        await self.bot.store.event("command_admin", interaction.user.id)
        if key != self.bot.config.admin_key:
            await self.bot.store.event("admin_rejected", interaction.user.id, detail="invalid_key")
            await interaction.response.send_message("管理キーが正しくありません。",ephemeral=True); return
        await self.bot.store.query("UPDATE users SET perm_name='admin' WHERE dc_user_id=%s",(str(interaction.user.id),)); await self.bot.store.event("admin_granted",interaction.user.id)
        await interaction.response.send_message("admin 権限を有効化しました。",ephemeral=True)
    @app_commands.command(description="EULA 同意後にハードコアサーバーを作成します")
    @app_commands.describe(name="admin は作成するサーバー名を必ず入力")
    async def create(self, interaction, name: str | None = None):
        await self.user(interaction)
        await self.bot.store.event("command_create", interaction.user.id)
        if await self.bot.store.permission(interaction.user.id) == "admin":
            if not name:
                await self.bot.store.event("create_rejected", interaction.user.id, detail="admin_name_required")
                await interaction.response.send_message("admin は `name` を入力してサーバー名を指定してください。",ephemeral=True); return
            if not NAME_RE.fullmatch(name):
                await self.bot.store.event("create_rejected", interaction.user.id, detail="invalid_name")
                await interaction.response.send_message("名前は英数字・`_`・`-` の 3〜32 文字にしてください。",ephemeral=True); return
        else:
            name=f"hc-{interaction.user.id}"
        await interaction.response.send_message("Minecraft の EULA に同意しますか？\nhttps://aka.ms/MinecraftEULA",view=EulaView(self.bot,interaction.user.id,name),ephemeral=True)
    @app_commands.command(description="ワールドをリセットし、削除期限を延長します")
    @app_commands.describe(code="他ユーザーのサーバーをリセットする場合に入力")
    async def reset(self, interaction, code: str | None = None):
        await self.user(interaction)
        await self.bot.store.event("command_reset", interaction.user.id, detail="mode=code" if code else "mode=own")
        if code:
            server=await self.bot.store.server_for_reset_code(code)
            if not server:
                await self.bot.store.event("reset_rejected", interaction.user.id, detail="invalid_code")
                await interaction.response.send_message("リセットコードが正しくないか、対象サーバーは利用できません。",ephemeral=True); return
            await interaction.response.send_message("このサーバーのワールドをリセットしますか？",view=ResetConfirmView(self.bot,interaction.user.id,server),ephemeral=True)
            return
        servers=await self.bot.store.servers_for_user(interaction.user.id)
        if not servers:
            await self.bot.store.event("reset_rejected", interaction.user.id, detail="no_owned_server")
            await interaction.response.send_message("リセットできる自分のサーバーはありません。",ephemeral=True); return
        if len(servers) == 1:
            await interaction.response.send_message("このサーバーのワールドをリセットしますか？",view=ResetConfirmView(self.bot,interaction.user.id,servers[0]),ephemeral=True); return
        await interaction.response.send_message("リセットする自分のサーバーを選択してください。",view=ResetSelectView(self.bot,interaction.user.id,servers),ephemeral=True)
    @app_commands.command(description="自分のサーバーの起動状態と削除予定を表示します")
    async def status(self, interaction):
        await self.user(interaction)
        await self.bot.store.event("command_status", interaction.user.id)
        servers=await self.bot.store.servers_for_user(interaction.user.id)
        if not servers:
            await self.bot.store.event("status_empty", interaction.user.id)
            await interaction.response.send_message("作成済みのサーバーはありません。",ephemeral=True); return
        await interaction.response.defer(ephemeral=True,thinking=True)
        checks=await asyncio.gather(*[asyncio.to_thread(self.bot.remote.run,"status",server["sv_port"]) for server in servers],return_exceptions=True)
        jst=timezone(timedelta(hours=9))
        embed=discord.Embed(title="サーバー状態",color=discord.Color.green())
        for server, check in zip(servers,checks):
            state="起動中" if check is True else ("停止中" if check is False else "状態確認失敗")
            if server["last_reset_at"]:
                deleted_at=server["last_reset_at"].replace(tzinfo=timezone.utc)+timedelta(days=self.bot.config.retention_days)
                deadline=deleted_at.astimezone(jst).strftime("%Y-%m-%d %H:%M JST")
            else: deadline="未設定"
            address = self.bot.address_for(server["sv_port"]) if server["sv_port"] is not None else "未割当"
            embed.add_field(name=f"{server['sv_name']} — {state}",value=f"接続先: `{address}`\nバージョン: `{server['sv_type']} {server['sv_ver']}`\nリセットコード: ||{server['reset_code'] or '未発行'}||\n自動削除予定: `{deadline}`",inline=False)
            await self.bot.store.event("status_checked", interaction.user.id, server["sv_id"], f"state={state}")
        await interaction.followup.send(embed=embed,ephemeral=True)
        await self.bot.store.event("status_completed", interaction.user.id, detail=f"servers={len(servers)}")
    @app_commands.command(description="サーバーを削除し、作成枠を解放します")
    async def delete(self, interaction):
        await self.user(interaction)
        await self.bot.store.event("command_delete", interaction.user.id)
        servers=await self.bot.store.servers_for_user(interaction.user.id)
        if not servers:
            await self.bot.store.event("delete_rejected", interaction.user.id, detail="no_owned_server")
            await interaction.response.send_message("削除できるサーバーはありません。",ephemeral=True); return
        if len(servers) == 1:
            await interaction.response.send_message("このサーバーを完全に削除しますか？",view=DeleteConfirmView(self.bot,interaction.user.id,servers[0]),ephemeral=True); return
        await interaction.response.send_message("削除するサーバーを選択してください。",view=DeleteSelectView(self.bot,interaction.user.id,servers),ephemeral=True)
    @app_commands.command(description="MinecraftユーザーにOP権限を付与します")
    async def op(self, interaction):
        await self.user(interaction)
        await self.bot.store.event("command_op", interaction.user.id)
        servers=await self.bot.store.servers_for_user(interaction.user.id)
        if not servers:
            await self.bot.store.event("op_rejected", interaction.user.id, detail="no_owned_server")
            await interaction.response.send_message("OP権限を付与できる自分のサーバーはありません。",ephemeral=True); return
        await interaction.response.send_message("OP権限を付与するサーバーを選択してください。", view=OpSelectView(self.bot, interaction.user.id, servers), ephemeral=True)

if __name__ == "__main__":
    load_dotenv(); config=Config.load(); Bot(config).run(config.token,log_handler=None)
