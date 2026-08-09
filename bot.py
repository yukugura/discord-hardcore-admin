"""DB を正として Discord から Minecraft ハードコア鯖を提供するボット。"""
import asyncio, logging, os, re, secrets, shlex, string
from dataclasses import dataclass

import discord
import mysql.connector
import paramiko
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
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
    async def reserve(self, user_id, name, server_type, version):
        async with self.port_lock:
            perm = await self.permission(user_id)
            if perm == "default":
                active = await self.query("SELECT sv_id FROM servers WHERE dc_user_id=%s AND status<>'deleted'", (str(user_id),), True)
                if active: return None
            duplicate = await self.query("SELECT sv_id FROM servers WHERE dc_user_id=%s AND sv_name=%s AND status<>'deleted'", (str(user_id), name), True)
            if duplicate: raise ValueError("同じ名前のサーバーがすでに存在します")
            used = {r["sv_port"] for r in await self.query("SELECT sv_port FROM servers WHERE status<>'deleted'", fetch=True)}
            port = next((p for p in range(self.config.min_port, self.config.max_port + 1) if p not in used), None)
            if port is None:
                await self.event("create_rejected_capacity", user_id, detail="all 10 hardcore slots are occupied")
                raise CapacityError("現在、ハードコアサーバーの作成枠（10 台）がすべて使用中です。空きが出るまで作成できません。")
            # コードはパスワードではなく、共有リセット用の識別子として平文保存する。
            alphabet = string.ascii_uppercase + string.digits
            for _ in range(10):
                reset_code = ''.join(secrets.choice(alphabet) for _ in range(8))
                try:
                    await self.query("INSERT INTO servers(dc_user_id,sv_name,sv_type,sv_ver,sv_port,status,last_reset_at,reset_code) VALUES(%s,%s,%s,%s,%s,'creating',UTC_TIMESTAMP(),%s)", (str(user_id),name,server_type,version,port,reset_code))
                    break
                except mysql.connector.IntegrityError:
                    continue
            else: raise RuntimeError("リセットコードを発行できませんでした")
            row = (await self.query("SELECT sv_id FROM servers WHERE dc_user_id=%s AND sv_name=%s", (str(user_id),name), True))[0]
            await self.event("create_requested", user_id, row["sv_id"], f"{server_type} {version}; port={port}")
            return {"id":row["sv_id"],"port":port,"reset_code":reset_code}
    async def create_result(self, server, user_id, ok):
        await self.query("UPDATE servers SET status=%s WHERE sv_id=%s", ("running" if ok else "error",server["id"]))
        await self.event("created" if ok else "create_failed", user_id, server["id"])
    async def get_server(self, user_id, name):
        rows = await self.query("SELECT sv_id,sv_port FROM servers WHERE dc_user_id=%s AND sv_name=%s AND status<>'deleted'", (str(user_id),name), True)
        return rows[0] if rows else None
    async def server_for_reset_code(self, reset_code):
        rows = await self.query("SELECT sv_id,sv_port FROM servers WHERE reset_code=%s AND status IN ('running','stopped','error')", (reset_code.upper(),), True)
        return rows[0] if rows else None
    async def servers_for_user(self, user_id):
        return await self.query("SELECT sv_id,sv_name,sv_port FROM servers WHERE dc_user_id=%s AND status<>'deleted' ORDER BY sv_id", (str(user_id),), True)
    async def reset_result(self, server, user_id, ok):
        if ok: await self.query("UPDATE servers SET status='running',last_reset_at=UTC_TIMESTAMP() WHERE sv_id=%s", (server["sv_id"],))
        await self.event("reset" if ok else "reset_failed", user_id, server["sv_id"])
    async def expired(self):
        return await self.query("SELECT sv_id,dc_user_id,sv_port FROM servers WHERE status IN ('running','stopped','error') AND last_reset_at < UTC_TIMESTAMP() - INTERVAL %s DAY", (self.config.retention_days,), True)
    async def deleted(self, server, ok):
        if ok: await self.query("UPDATE servers SET status='deleted',sv_port=NULL WHERE sv_id=%s", (server["sv_id"],))
        await self.event("expired_deleted" if ok else "expired_delete_failed", None, server["sv_id"], f"owner={server['dc_user_id']}")
    async def manual_deleted(self, server, user_id, ok):
        if ok: await self.query("UPDATE servers SET status='deleted',sv_port=NULL WHERE sv_id=%s", (server["sv_id"],))
        await self.event("deleted" if ok else "delete_failed", user_id, server["sv_id"])

class Remote:
    def __init__(self, config): self.config = config
    def run(self, action, port, server_type=None, version=None, url=None):
        if action not in {"create","reset","delete"} or not self.config.min_port <= port <= self.config.max_port: raise ValueError("不正な管理操作")
        args = [action, str(port)]
        if action == "create":
            if server_type not in {"vanilla","paper"} or not re.fullmatch(r"[0-9.]+", version or "") or not (url or "").startswith("https://"): raise ValueError("不正な作成情報")
            args += [server_type, version, url]
        client = paramiko.SSHClient(); client.load_system_host_keys(); client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(**self.config.ssh, look_for_keys=not bool(self.config.ssh["password"]), allow_agent=False, timeout=15)
            command = "sudo -n /usr/local/sbin/hardcore-pool-admin " + " ".join(shlex.quote(a) for a in args)
            _, out, err = client.exec_command(command, timeout=300); code = out.channel.recv_exit_status()
            detail = (out.read()+err.read()).decode(errors="replace").strip()
            if code: log.error("remote %s failed: %s", action, detail)
            return code == 0
        finally: client.close()

async def provision(bot, interaction, name, server_type, version_data):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try: server = await bot.store.reserve(interaction.user.id, name, server_type, version_data["sv_ver"])
    except (ValueError, CapacityError) as e: await interaction.followup.send(str(e), ephemeral=True); return
    except Exception: log.exception("reserve failed"); await interaction.followup.send("作成枠を確保できませんでした。", ephemeral=True); return
    if server is None:
        await interaction.followup.send("すでにサーバーを作成済みです。default 権限では 1 人 1 台までです。", ephemeral=True); return
    url = version_data["download_url"] if server_type == "paper" else "https://example.invalid/unused.jar"
    try: ok = await asyncio.to_thread(bot.remote.run, "create", server["port"], server_type, version_data["sv_ver"], url)
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
        await interaction.response.send_message("Paper のバージョンを選択してください。", view=VersionView(self.bot,self.owner,self.name,versions), ephemeral=True); self.stop()
    @discord.ui.button(label="なし（最新 Vanilla）", style=discord.ButtonStyle.secondary)
    async def vanilla(self, interaction, _):
        try: version = await self.bot.store.latest("vanilla")
        except Exception: log.exception("latest failed"); await interaction.response.send_message("最新バージョンを取得できませんでした。", ephemeral=True); return
        self.stop(); await provision(self.bot, interaction, self.name, "vanilla", version)

class VersionView(OwnerView):
    def __init__(self, bot, owner, name, versions):
        super().__init__(bot,owner,name); self.versions={v["sv_ver"]:v for v in versions}
        self.add_item(VersionSelect(self.versions))
    async def chosen(self, interaction, version): self.stop(); await provision(self.bot, interaction, self.name, "paper", self.versions[version])
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
    @cleanup.before_loop
    async def before_cleanup(self): await self.wait_until_ready()

class ControlCog(commands.Cog):
    def __init__(self, bot): self.bot=bot
    async def user(self, interaction): await self.bot.store.ensure_user(interaction.user)
    @app_commands.command(description="管理者権限を有効化します")
    async def admin(self, interaction, key: str):
        await self.user(interaction)
        if key != self.bot.config.admin_key: await interaction.response.send_message("管理キーが正しくありません。",ephemeral=True); return
        await self.bot.store.query("UPDATE users SET perm_name='admin' WHERE dc_user_id=%s",(str(interaction.user.id),)); await self.bot.store.event("admin_granted",interaction.user.id)
        await interaction.response.send_message("admin 権限を有効化しました。",ephemeral=True)
    @app_commands.command(description="EULA 同意後にハードコアサーバーを作成します")
    async def create(self, interaction):
        await self.user(interaction)
        name=f"hc-{interaction.user.id}"
        await interaction.response.send_message("Minecraft の EULA に同意しますか？\nhttps://aka.ms/MinecraftEULA",view=EulaView(self.bot,interaction.user.id,name),ephemeral=True)
    @app_commands.command(name="admin-create", description="admin 用: 名前を指定して追加のサーバーを作成します")
    async def admin_create(self, interaction, name: str):
        await self.user(interaction)
        if await self.bot.store.permission(interaction.user.id) != "admin":
            await interaction.response.send_message("このコマンドは admin 専用です。",ephemeral=True); return
        if not NAME_RE.fullmatch(name): await interaction.response.send_message("名前は英数字・`_`・`-` の 3〜32 文字にしてください。",ephemeral=True); return
        await interaction.response.send_message("Minecraft の EULA に同意しますか？\nhttps://aka.ms/MinecraftEULA",view=EulaView(self.bot,interaction.user.id,name),ephemeral=True)
    @app_commands.command(description="ワールドをリセットし、削除期限を延長します")
    async def reset(self, interaction, code: str):
        await self.user(interaction)
        server=await self.bot.store.server_for_reset_code(code)
        if not server: await interaction.response.send_message("リセットコードが正しくないか、対象サーバーは利用できません。",ephemeral=True); return
        await interaction.response.send_message("このサーバーのワールドをリセットしますか？",view=ResetConfirmView(self.bot,interaction.user.id,server),ephemeral=True)
    @app_commands.command(description="サーバーを削除し、作成枠を解放します")
    async def delete(self, interaction):
        await self.user(interaction)
        servers=await self.bot.store.servers_for_user(interaction.user.id)
        if not servers: await interaction.response.send_message("削除できるサーバーはありません。",ephemeral=True); return
        if len(servers) == 1:
            await interaction.response.send_message("このサーバーを完全に削除しますか？",view=DeleteConfirmView(self.bot,interaction.user.id,servers[0]),ephemeral=True); return
        await interaction.response.send_message("削除するサーバーを選択してください。",view=DeleteSelectView(self.bot,interaction.user.id,servers),ephemeral=True)

if __name__ == "__main__":
    load_dotenv(); config=Config.load(); Bot(config).run(config.token,log_handler=None)
