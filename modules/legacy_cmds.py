# -*- coding: utf-8 -*-
import asyncio
import gc
import os
import re
import shutil
import json
import traceback
from zipfile import ZipFile

from typing import Union, Optional
import disnake
import dotenv
import psutil
import wavelink
from disnake.ext import commands
from aiohttp import ClientSession

from utils.client import BotCore
from utils.db import DBModel
from utils.music.checks import check_voice, check_requester_channel
from utils.music.models import LavalinkPlayer
from utils.others import sync_message, CustomContext, string_to_file, token_regex, CommandArgparse
from utils.owner_panel import panel_command, PanelView
from utils.music.errors import GenericError
from config_loader import DEFAULT_CONFIG, load_config


def format_git_log(data_list: list):

    data = []

    for d in data_list:
        if not d:
            continue
        t = d.split("*****")
        data.append({"commit": t[0], "abbreviated_commit": t[1], "subject": t[2], "timestamp": t[3]})

    return data


async def run_command(cmd: str):

    p = await asyncio.create_subprocess_shell(
        cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, stderr = await p.communicate()
    r = ShellResult(p.returncode, stdout, stderr)
    if r.status != 0:
        raise GenericError(f"{r.stderr or r.stdout}\n\nStatus Code: {r.status}")
    return str(r.stdout)


class ShellResult:

    def __init__(self, status: int, stdout: Optional[bytes], stderr: Optional[bytes]):
        self.status = status
        self.stdout = stdout.decode(encoding="utf-8", errors="replace") if stdout is not None else None
        self.stderr = stderr.decode(encoding="utf-8", errors="replace") if stderr is not None else None


class Owner(commands.Cog):

    os_quote = "\"" if os.name == "nt" else "'"
    git_format = f"--pretty=format:{os_quote}%H*****%h*****%s*****%ct{os_quote}"

    extra_files = [
        "./playlist_cache.json",
    ]

    additional_files = [
        "./lavalink.ini",
        "./application.yml",
        "./squarecloud.config",
        "./squarecloud.app",
        "./discloud.config",
    ]

    extra_dirs = [
        "local_database",
        ".player_sessions"
    ]

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.git_init_cmds = [
            "git init",
            f'git remote add origin {self.bot.config["SOURCE_REPO"]}',
            'git fetch origin',
            'git checkout -b main -f --track origin/main'
        ]
        self.owner_view: Optional[PanelView] = None
        self.extra_hints = bot.config["EXTRA_HINTS"].split("||")

    def format_log(self, data: list):
        return "\n".join(f"[`{c['abbreviated_commit']}`]({self.bot.pool.remote_git_url}/commit/{c['commit']}) `- "
                         f"{(c['subject'][:40].replace('`', '') + '...') if len(c['subject']) > 39 else c['subject']}` "
                         f"(<t:{c['timestamp']}:R>)" for c in data)

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.is_owner()
    @commands.command(
        hidden=True, aliases=["gls", "lavalink", "lllist", "lavalinkservers"],
        description="Baixar um arquivo com lista de servidores lavalink para usá-los no sistema de música."
    )
    async def getlavaservers(self, ctx: CustomContext):

        await ctx.defer()

        await self.download_lavalink_serverlist()

        await ctx.send(
            embed=disnake.Embed(
                description="**O arquivo lavalink.ini foi baixado com sucesso!\n"
                            "Será necessário me reiniciar para usar os servidores deste arquivo.**"
            )
        )

    updatelavalink_flags = CommandArgparse()
    updatelavalink_flags.add_argument('-force', '--force', action='store_true',
                                      help="Ignorar a execução/uso do servidor LOCAL.")
    updatelavalink_flags.add_argument('-yml', '--yml', action='store_true',
                                      help="Fazer download do arquivo application.yml.")
    updatelavalink_flags.add_argument("-resetids", "-reset", "--resetids", "--reset",
                                      help="Resetar info de ids das músicas (útil pra evitar problemas com certas "
                                           "mudanças do lavaplayer/lavalink).", action="store_true")

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.user)
    @commands.command(hidden=True, aliases=["ull", "updatell", "llupdate", "llu"], extras={"flags": updatelavalink_flags})
    async def updatelavalink(self, ctx: CustomContext, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        node: Optional[wavelink.Node] = None

        for bot in self.bot.pool.bots:
            try:
                node = bot.music.nodes["LOCAL"]
                break
            except KeyError:
                continue

        if not node and not args.force:
            raise GenericError("**O servidor LOCAL não está sendo usado!**")

        download_urls = [self.bot.config["LAVALINK_FILE_URL"]]

        if args.yml:
            download_urls.append("https://github.com/zRitsu/LL-binaries/releases/download/0.0.1/application.yml")

        async with ctx.typing():

            for url in download_urls:
                async with ClientSession() as session:
                    async with session.get(url) as r:
                        lavalink_jar = await r.read()
                        with open(url.split("/")[-1], "wb") as f:
                            f.write(lavalink_jar)

        if node:

            for bot in self.bot.pool.bots:

                try:
                    node = bot.music.nodes["LOCAL"]
                except KeyError:
                    continue

                node.restarting = True

                for player in node.players.values():

                    txt = "O servidor de música foi reiniciado e a música será retomada em alguns segundos (Por favor aguarde)..."

                    if args.resetids:

                        if player.current:
                            player.queue.appendleft(player.current)
                            player.current = None

                        for t in player.queue:
                            t.id = ""

                        for t in player.played:
                            t.id = ""

                    if player.static or player.controller_mode:
                        player.set_command_log(text=txt, emoji="🛠️")
                        bot.loop.create_task(player.invoke_np(force=True))
                    else:
                        bot.loop.create_task(
                            player.text_channel.send(
                                embed=disnake.Embed(
                                    color=self.bot.get_color(player.guild.me),
                                    description=f"🛠️ **⠂{txt}**"
                                )
                            )
                        )

        self.bot.pool.start_lavalink()

        await ctx.send(
            embed=disnake.Embed(
                description="**O arquivo Lavalink.jar foi atualizado com sucesso!**",
                color=self.bot.get_color(ctx.guild.me)
            )
        )

    @commands.is_owner()
    @panel_command(aliases=["rd", "recarregar"], description="Recarregar os módulos.", emoji="🔄",
                   alt_name="Carregar/Recarregar módulos.")
    async def reload(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        data = self.bot.load_modules()

        await self.bot.sync_app_commands()

        for bot in self.bot.pool.bots:

            if bot.user.id != self.bot.user.id:
                bot.load_modules()
                await bot.sync_app_commands()

        self.bot.sync_command_cooldowns()

        txt = ""

        if data["loaded"]:
            txt += f'**Módulos carregados:** ```ansi\n[0;34m{" [0;37m| [0;34m".join(data["loaded"])}```\n'

        if data["reloaded"]:
            txt += f'**Módulos recarregados:** ```ansi\n[0;32m{" [0;37m| [0;32m".join(data["reloaded"])}```\n'

        if not txt:
            txt = "**Nenhum módulo encontrado...**"

        self.bot.pool.config = load_config()

        gc.collect()

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt)
            await ctx.send(embed=embed, view=self.owner_view)
        else:
            return txt

    update_flags = CommandArgparse()
    update_flags.add_argument("-force", "--force", action="store_true",
                              help="Forçar update ignorando o estado do repositório local).")
    update_flags.add_argument("-pip", "--pip", action="store_true",
                              help="Instalar/atualizar dependências após a atualização.")

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @panel_command(aliases=["up", "atualizar"], description="Atualizar meu code usando o git.",
                   emoji="<:git:944873798166020116>", alt_name="Atualizar Bot", extras={"flags": update_flags})
    async def update(self, ctx: Union[CustomContext, disnake.MessageInteraction], *,
                     opts: str = ""):  # TODO: Rever se há alguma forma de usar commands.Flag sem um argumento obrigatório, ex: --pip.

        out_git = ""

        git_log = []

        if shutil.which("poetry"):
            file = "./pyproject.toml"
            use_poetry = True
        else:
            file = "./requirements.txt"
            use_poetry = False

        requirements_old = ""
        try:
            with open(file) as f:
                requirements_old = f.read()
        except:
            pass

        args, unknown = self.bot.get_command("update").extras['flags'].parse_known_args(opts.split())

        if not os.path.isdir("./.git") or args.force:

            out_git += await self.cleanup_git(force=args.force)

        else:

            try:
                await ctx.response.defer()
            except:
                pass

            try:
                await run_command("git reset --hard")
            except:
                pass

            try:
                pull_log = await run_command("git pull --allow-unrelated-histories -X theirs")
                if "Already up to date" in pull_log:
                    raise GenericError("**Já estou com os ultimos updates instalados...**")
                out_git += pull_log

            except GenericError as e:
                raise e

            except Exception as e:

                if "Already up to date" in str(e):
                    raise GenericError("Já estou com os ultimos updates instalados...")

                elif not "Fast-forward" in str(e):
                    out_git += await self.cleanup_git(force=True)

            commit = ""

            for l in out_git.split("\n"):
                if l.startswith("Updating"):
                    commit = l.replace("Updating ", "").replace("..", "...")
                    break

            data = (await run_command(f"git log {commit} {self.git_format}")).split("\n")

            git_log += format_git_log(data)

        text = "`Será necessário me reiniciar após as alterações.`"

        txt = f"`✅` **[Atualização realizada com sucesso!]({self.bot.pool.remote_git_url}/commits/main)**"

        if git_log:
            txt += f"\n\n{self.format_log(git_log[:10])}"

        txt += f"\n\n`📄` **Log:** ```py\n{out_git[:1000].split('Fast-forward')[-1]}```\n{text}"

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(
                description=txt,
                color=self.bot.get_color(ctx.guild.me)
            )
            await ctx.send(embed=embed, view=self.owner_view)

            self.bot.loop.create_task(self.update_deps(ctx, requirements_old, args, use_poetry=use_poetry))

        else:
            self.bot.loop.create_task(self.update_deps(ctx, requirements_old, args, use_poetry=use_poetry))
            return txt

    async def update_deps(self, ctx, original_reqs, args, use_poetry=False):

        if use_poetry:
            cmd = "poetry install"
            file = "./pyproject.toml"
        else:
            cmd = "pip3 install -U -r requirements.txt --no-cache-dir"
            file = "./requirements.txt"

        if args.pip:

            embed = disnake.Embed(
                description="**Instalando as dependências.\nPor favor aguarde...**",
                color=self.bot.get_color(ctx.guild.me)
            )

            msg = await ctx.channel.send(embed=embed)

            await run_command(cmd)

            embed.description = "**As dependências foram instaladas com sucesso!**"

            await msg.edit(embed=embed)

        else:

            with open(file) as f:
                requirements_new = f.read()

            if original_reqs != requirements_new:

                txt = ""

                if venv:=os.getenv("VIRTUAL_ENV"):
                    if os.name == "nt":
                        txt += "call " + venv.split('\\')[-1] + " && "
                    else:
                        txt += ". ./" + venv.split('/')[-1] + " && "

                try:
                    prefix = ctx.prefix if (not str(ctx.guild.me.id) in ctx.prefix) else f"@{ctx.guild.me.name}"
                except AttributeError:
                    prefix = self.bot.default_prefix if self.bot.intents.message_content else f"@{ctx.guild.me.name}"

                await ctx.send(
                    embed=disnake.Embed(
                        description="**Será necessário atualizar as dependências usando o comando "
                                    "abaixo no terminal/shell:**\n"
                                    f"```sh\n{txt}{cmd}```\nou usar usar o comando: "
                                    f"```ansi\n[34;1m{prefix}update --force --pip[0m``` \n"
                                    f"**Nota:** Dependendo da hospedagem (ou que não tenha 150mb de RAM livre "
                                    f"e 0.5vCPU) você deve enviar o arquivo requirements.txt ao invés de "
                                    f"usar uma das opções acima ou os botões de instalar dependências abaixo...",
                        color=self.bot.get_color(ctx.guild.me)
                    ),
                    components=[
                        disnake.ui.Button(label="Download requirements.txt", custom_id="updatecmd_requirements"),
                        disnake.ui.Button(label="Atualizar dependências",
                                          custom_id="updatecmd_installdeps_" + ("poetry" if use_poetry else "pip")),
                        disnake.ui.Button(label="Atualizar dependências (force)",
                                          custom_id="updatecmd_installdeps_force_" + ("poetry" if use_poetry else "pip")),
                    ]
                )

    @commands.Cog.listener("on_button_click")
    async def update_buttons(self, inter: disnake.MessageInteraction):

        if not inter.data.custom_id.startswith("updatecmd_"):
            return

        if inter.data.custom_id.startswith("updatecmd_requirements"):

            try:
                os.remove('./update_reqs.zip')
            except FileNotFoundError:
                pass

            with ZipFile('update_reqs.zip', 'w') as zipObj:
                zipObj.write("requirements.txt")

            await inter.send(
                embed=disnake.Embed(
                    description="**Baixe o arquivo anexado e envie para sua hospedagem via commit etc.**",
                    color=self.bot.get_color(inter.guild.me)
                ),
                file=disnake.File("update_reqs.zip")
            )

            os.remove("update_reqs.zip")
            return

        # install installdeps

        if inter.data.custom_id.startswith("updatecmd_installdeps_force_"):
            await self.cleanup_git(force=True)

        await inter.message.delete()

        args, unknown = self.bot.get_command("update").extras['flags'].parse_known_args(["-pip"])

        await self.update_deps(inter, "", args, use_poetry=inter.data.custom_id.endswith("_poetry"))

    async def cleanup_git(self, force=False):

        if force:
            try:
                shutil.rmtree("./.git")
            except FileNotFoundError:
                pass

        out_git = ""

        for c in self.git_init_cmds:
            try:
                out_git += (await run_command(c)) + "\n"
            except Exception as e:
                out_git += f"{e}\n"

        self.bot.pool.commit = (await run_command("git rev-parse HEAD")).strip("\n")
        self.bot.pool.remote_git_url = self.bot.config["SOURCE_REPO"][:-4]

        return out_git

    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.cooldown(1, 10, commands.BucketType.user)
    @panel_command(aliases=["latest", "lastupdate"], description="Ver minhas atualizações mais recentes.", emoji="📈",
                   alt_name="Ultimas atualizações", hidden=False)
    async def updatelog(self, ctx: Union[CustomContext, disnake.MessageInteraction], amount: int = 10):

        if not os.path.isdir("./.git"):
            raise GenericError("Não há repositorio iniciado no diretório do bot...\nNota: Use o comando update.")

        if not self.bot.pool.remote_git_url:
            self.bot.pool.remote_git_url = self.bot.config["SOURCE_REPO"][:-4]

        git_log = []

        data = (await run_command(f"git log -{amount or 10} {self.git_format}")).split("\n")

        git_log += format_git_log(data)

        txt = f"🔰 ** | [Atualizações recentes:]({self.bot.pool.remote_git_url}/commits/main)**\n\n" + self.format_log(
            git_log)

        if isinstance(ctx, CustomContext):

            embed = disnake.Embed(
                description=txt,
                color=self.bot.get_color(ctx.guild.me)
            )

            await ctx.send(embed=embed, view=self.owner_view if (await self.bot.is_owner(ctx.author)) else None)

        else:
            return txt

    @commands.is_owner()
    @commands.command(hidden=True, aliases=["menu"])
    async def panel(self, ctx: CustomContext):

        embed =disnake.Embed(
            title="PAINEL DE CONTROLE.",
            color=self.bot.get_color(ctx.guild.me)
        )
        embed.set_footer(text="Clique em uma tarefa que deseja executar.")
        await ctx.send(embed=embed, view=PanelView(self.bot))

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(description="Sincronizar/Registrar os comandos de barra no servidor.", hidden=True)
    async def syncguild(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        embed = disnake.Embed(
            color=self.bot.get_color(ctx.guild.me),
            description="**Esse comando não é mais necessário ser usado (A sincronização dos comandos agora "
                        f"é automática).**\n\n{sync_message(self.bot)}"
        )

        await ctx.send(embed=embed)

    @commands.is_owner()
    @panel_command(aliases=["sync"], description="Sincronizar os comandos de barra manualmente.",
                   emoji="<:slash:944875586839527444>",
                   alt_name="Sincronizar comandos manualmente.")
    async def synccmds(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        if self.bot.config["AUTO_SYNC_COMMANDS"] is True:
            raise GenericError(
                f"**Isso não pode ser usado com a sincronização automática ativada...**\n\n{sync_message(self.bot)}")

        await self.bot._sync_application_commands()

        txt = f"**Os comandos de barra foram sincronizados com sucesso!**\n\n{sync_message(self.bot)}"

        if isinstance(ctx, CustomContext):

            embed = disnake.Embed(
                color=self.bot.get_color(ctx.guild.me),
                description=txt
            )

            await ctx.send(embed=embed, view=self.owner_view)

        else:
            return txt

    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["mudarprefixo", "prefix", "changeprefix"],
        description="Alterar o prefixo do servidor",
        usage="{prefix}{cmd} [prefixo]\nEx: {prefix}{cmd} >>"
    )
    async def setprefix(self, ctx: CustomContext, prefix: str):

        if " " in prefix or len(prefix) > 5:
            raise GenericError("**O prefixo não pode conter espaços ou ter acima de 5 caracteres.**")

        try:
            guild_data = ctx.global_guild_data
        except AttributeError:
            guild_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)
            ctx.global_guild_data = guild_data

        guild_data["prefix"] = prefix
        await self.bot.update_global_data(ctx.guild.id, guild_data, db_name=DBModel.guilds)

        prefix = disnake.utils.escape_markdown(prefix)

        embed = disnake.Embed(
            description=f"**O prefixo deste servidor agora é:** `{prefix}`\n"
                        f"**Caso queira restaurar o prefixo padrão use o comando:** `{prefix}{self.resetprefix.name}`",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        description="Resetar o prefixo do servidor (Usar o prefixo padrão do bot)"
    )
    async def resetprefix(self, ctx: CustomContext):

        try:
            guild_data = ctx.global_guild_data
        except AttributeError:
            guild_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)
            ctx.global_guild_data = guild_data

        if not guild_data["prefix"]:
            raise GenericError("**Nao há prefixo configurado no servidor.**")

        guild_data["prefix"] = ""

        await self.bot.update_global_data(ctx.guild.id, guild_data, db_name=DBModel.guilds)

        embed = disnake.Embed(
            description=f"**O prefixo do servidor foi resetado com sucesso.\n"
                        f"O prefixo padrão agora é:** `{disnake.utils.escape_markdown(self.bot.default_prefix)}`",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["uprefix", "spu", "setmyprefix", "spm", "setcustomprefix", "scp", "customprefix", "myprefix"],
        description="Alterar seu prefixo de usuário (prefixo que irei responder a você independente "
                    "do prefixo configurado no servidor).",
        usage="{prefix}{cmd} [prefixo]\nEx: {prefix}{cmd} >>"
    )
    async def setuserprefix(self, ctx: CustomContext, prefix: str):

        if " " in prefix or len(prefix) > 5:
            raise GenericError("**O prefixo não pode conter espaços ou ter acima de 5 caracteres.**")

        try:
            user_data = ctx.global_user_data
        except AttributeError:
            user_data = await self.bot.get_global_data(ctx.author.id, db_name=DBModel.users)
            ctx.global_user_data = user_data

        user_data["custom_prefix"] = prefix
        self.bot.pool.user_prefix_cache[ctx.author.id] = prefix
        await self.bot.update_global_data(ctx.author.id, user_data, db_name=DBModel.users)

        prefix = disnake.utils.escape_markdown(prefix)

        embed = disnake.Embed(
            description=f"**O seu prefixo de usuário agora é:** `{prefix}`\n"
                        f"**Caso queira remover seu prefixo de usuário use o comando:** `{prefix}{self.resetuserprefix.name}`",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(description="Remover seu prefixo de usuário")
    async def resetuserprefix(self, ctx: CustomContext):

        try:
            user_data = ctx.global_user_data
        except AttributeError:
            user_data = await self.bot.get_global_data(ctx.author.id, db_name=DBModel.users)
            ctx.global_user_data = user_data

        if not user_data["custom_prefix"]:
            raise GenericError("**Você não possui prefixo configurado.**")

        user_data["custom_prefix"] = ""
        self.bot.pool.user_prefix_cache[ctx.author.id] = ""
        await self.bot.update_global_data(ctx.author.id, user_data, db_name=DBModel.users)

        embed = disnake.Embed(
            description=f"**O seu prefixo de usuário foi removido com sucesso.**",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command(
        aliases=["guildprefix", "sgp", "gp"], hidden=True,
        description="Setar um prefixo manualmente pra um server com o id informado (útil para botlists)",
        usage="{prefix}{cmd} [server id] <prefixo>\nEx: {prefix}{cmd} 1155223334455667788 >>\nNota: Use o comando sem especificar um prefix para removê-lo."
    )
    async def setguildprefix(self, ctx: CustomContext, server_id: int, prefix: str = None):

        if not 17 < len(str(server_id)) < 24:
            raise GenericError("**A quantidade de caracteres do id do servidor tem que estar entre 18 a 23.**")

        guild_data = await self.bot.get_global_data(server_id, db_name=DBModel.guilds)

        embed = disnake.Embed(color=self.bot.get_color(ctx.guild.me))

        if not prefix:
            guild_data["prefix"] = ""
            await ctx.bot.update_global_data(server_id, guild_data, db_name=DBModel.guilds)
            embed.description = "**O prefixo antecipado do servidor com o id informado foi resetado com sucesso.**"

        else:
            guild_data["prefix"] = prefix
            await self.bot.update_global_data(server_id, guild_data, db_name=DBModel.guilds)
            embed.description = f"**O prefixo para o servidor com o id informado agora é:** {disnake.utils.escape_markdown(prefix)}"

        await ctx.send(embed=embed)

    @commands.is_owner()
    @panel_command(aliases=["expsource", "export", "exs"],
                   description="Exportar minha source para um arquivo zip.", emoji="💾",
                   alt_name="Exportar source/código-fonte.")
    async def exportsource(self, ctx:Union[CustomContext, disnake.MessageInteraction], *, flags: str = ""):

        if not os.path.isdir("./.git"):
            await self.cleanup_git(force=True)

        try:
            env_file = dotenv.dotenv_values("./.env")
        except:
            env_file = {}

        try:
            with open("config.json") as f:
                config_json = json.load(f)
        except FileNotFoundError:
            config_json = {}

        SECRETS = dict(DEFAULT_CONFIG)
        SECRETS.update({"TOKEN": ""})

        for env, value in os.environ.items():
            if (e:=env.lower()).startswith(("token_bot_", "test_guilds_", "lavalink_node_")) or e == "token":
                SECRETS[env] = os.environ[env]
                continue

            if not isinstance(value, str):
                continue

            tokens = []

            for string in value.split():
                if re.findall(token_regex, value) and len(string) < 91:
                    tokens.append(string)

            if tokens:
                SECRETS[env] = value

        for i in SECRETS:
            try:
                SECRETS[i] = os.environ[i]
            except KeyError:
                continue

        SECRETS.update(config_json)
        SECRETS.update(env_file)

        if any(f in flags.lower() for f in ("-autodll", "--autodll")):
            SECRETS["AUTO_DOWNLOAD_LAVALINK_SERVERLIST"] = True

        if any(f in flags.lower() for f in ("--externalservers", "-externalservers", "--llservers", "-llservers", "--lls", "-lls")):
            await self.download_lavalink_serverlist()

        if not os.path.isfile("./.env-temp"):
            shutil.copyfile("./.example.env", "./.env-temp")

        for i in SECRETS:
            if not isinstance(SECRETS[i], str):
                SECRETS[i] = str(SECRETS[i]).lower()
            dotenv.set_key("./.env-temp", i, SECRETS[i])

        filelist = await run_command("git ls-files --others --exclude-standard --cached")

        if any(f in flags.lower() for f in ("--extradirs", "-extradirs", "--ed", "-ed", "--extrafiles", "-extrafiles", "--ef", "-ef")):
            for extra_dir in self.extra_dirs:
                for dir_path, dir_names, filenames in os.walk(extra_dir):
                    filelist += "\n" + "\n".join(os.path.join(dir_path, file) for file in filenames)

            for file in self.extra_files:
                if os.path.isfile(file):
                    filelist += "\n" + file

        for file in self.additional_files:
            if os.path.isfile(file):
                filelist += "\n" + file

        await self.bot.loop.run_in_executor(None, self.zip_dir, filelist.split("\n"))

        os.remove("./.env-temp")

        try:
            embed = disnake.Embed(
                description="**Não envie o arquivo source.zip ou o arquivo .env pra ninguém e muito cuidado ao postar "
                            "print's do conteudo do arquivo .env e não adicione esse arquivo em locais públicos como "
                            "github, repl.it, glitch.com, etc.**",
                color=self.bot.get_color(ctx.guild.me))
            embed.set_footer(text="Por medida de segurança, esta mensagem será deletada em 2 minutos.")

            msg = await ctx.author.send(
                embed=embed,
                file=disnake.File("./source.zip", filename=f"{self.bot.user}_source.zip"),
                delete_after=120
            )

            os.remove("./source.zip")

        except disnake.Forbidden:
            os.remove("./source.zip")
            raise GenericError("Seu DM está desativado!")

        if isinstance(ctx, CustomContext):
            await ctx.send(
                embed=disnake.Embed(
                    description=f"**O arquivo [source.zip]({msg.jump_url}) foi enviado no seu privado.**",
                    color=self.bot.get_color(ctx.guild.me)
                )
            )
        else:
            return f"Arquivo [source.zip]({msg.jump_url}) foi enviado com sucesso no seu DM."

    def zip_dir(self, filelist: list):

        try:
            os.remove("./source.zip")
        except:
            pass

        with ZipFile("./source.zip", 'a') as zipf:

            for f in filelist:
                if not f:
                    continue
                try:
                    if f == ".env-temp":
                        zipf.write('./.env-temp', './.env')
                    else:
                        zipf.write(f"./{f}")
                except FileNotFoundError:
                    continue

    @commands.is_owner()
    @commands.command(hidden=True)
    async def cleardm(self, ctx: CustomContext, amount: int = 20):

        counter = 0

        async with ctx.typing():

            async for msg in ctx.author.history(limit=int(amount)):
                if msg.author.id == self.bot.user.id:
                    await msg.delete()
                    await asyncio.sleep(0.5)
                    counter += 1

        if not counter:
            raise GenericError(f"**Nenhuma mensagem foi deletada de {amount} verificada(s)...**")

        if counter == 1:
            txt = "**Uma mensagem foi deletada do seu DM.**"
        else:
            txt = f"**{counter} mensagens foram deletadas do seu DM.**"

        await ctx.send(embed=disnake.Embed(description=txt, colour=self.bot.get_color(ctx.guild.me)))

    @commands.Cog.listener("on_button_click")
    async def close_shell_result(self, inter: disnake.MessageInteraction):

        if inter.data.custom_id != "close_shell_result":
            return

        if not await self.bot.is_owner(inter.author):
            return await inter.send("**Apenas meu dono pode usar este botão!**", ephemeral=True)

        await inter.response.edit_message(
            content="```ini\n🔒 - [Shell Fechado!] - 🔒```",
            attachments=None,
            view=None,
            embed=None
        )

    @commands.is_owner()
    @commands.command(aliases=["sh"], hidden=True)
    async def shell(self, ctx: CustomContext, *, command: str):

        if command.startswith('```') and command.endswith('```'):
            if command[4] != "\n":
                command = f"```\n{command[3:]}"
            if command[:-4] != "\n":
                command = command[:-3] + "\n```"
            command = '\n'.join(command.split('\n')[1:-1])
        else:
            command = command.strip('` \n')

        try:
            async with ctx.typing():
                result = await run_command(command)
        except GenericError as e:
            kwargs = {}
            if len(e.text) > 2000:
                kwargs["file"] = string_to_file(e.text, filename="error.txt")
            else:
                kwargs["content"] = f"```py\n{e.text}```"

            try:
                await ctx.author.send(**kwargs)
                await ctx.message.add_reaction("⚠️")
            except disnake.Forbidden:
                traceback.print_exc()
                raise GenericError(
                    "**Ocorreu um erro (verifique os logs/terminal ou libere seu DM para o próximo "
                    "resultado ser enviado diretamente no seu DM).**"
                )

        else:

            kwargs = {}
            if len(result) > 2000:
                kwargs["file"] = string_to_file(result, filename=f"shell_result_{ctx.message.id}.txt")
            else:
                kwargs["content"] = f"```py\n{result}```"

            await ctx.reply(
                components=[
                    disnake.ui.Button(label="Fechar Shell", custom_id="close_shell_result", emoji="♻️")
                ],
                mention_author=False,
                **kwargs
            )

    @check_voice()
    @commands.cooldown(1, 15, commands.BucketType.guild)
    @commands.command(description='inicializar um player no servidor.', aliases=["spawn", "sp", "spw", "smn"])
    async def summon(self, ctx: CustomContext):

        try:
            self.bot.music.players[ctx.guild.id]  # type ignore
            raise GenericError("**Já há um player iniciado no servidor.**")
        except KeyError:
            pass

        node: wavelink.Node = self.bot.music.get_best_node()

        if not node:
            raise GenericError("**Não há servidores de música disponível!**")

        try:
            guild_data = ctx.guild_data
        except AttributeError:
            guild_data = await self.bot.get_data(ctx.guild.id, db_name=DBModel.guilds)
            ctx.guild_data = guild_data

        try:
            global_data = ctx.global_guild_data
        except AttributeError:
            global_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)
            ctx.global_guild_data = global_data

        static_player = guild_data['player_controller']

        skin = guild_data["player_controller"]["skin"]
        static_skin = guild_data["player_controller"]["static_skin"]

        if global_data["global_skin"]:
            skin = global_data["player_skin"] or skin
            static_skin = global_data["player_skin_static"] or guild_data["player_controller"]["static_skin"]

        try:
            channel = self.bot.get_channel(int(static_player['channel'])) or await self.bot.fetch_channel(int(static_player['channel'])) or ctx.channel
        except (KeyError, TypeError, disnake.NotFound):
            channel = ctx.channel
            message = None
            static_player = False
        else:
            try:
                message = await channel.fetch_message(int(static_player.get('message_id')))
            except (TypeError, disnake.NotFound):
                message = None
            static_player = True

        try:
            invite = global_data["listen_along_invites"][str(ctx.channel.id)]
        except KeyError:
            invite = None

        else:
            try:
                await self.bot.fetch_invite(invite)
            except disnake.NotFound:
                invite = None
                del global_data["listen_along_invites"][str(ctx.channel.id)]
                await self.bot.update_global_data(ctx.guild_id, global_data, db_name=DBModel.guilds)

        player: LavalinkPlayer = self.bot.music.get_player(
            guild_id=ctx.guild_id,
            cls=LavalinkPlayer,
            player_creator=ctx.author.id,
            guild=ctx.guild,
            channel=channel or ctx.channel,
            last_message_id=guild_data['player_controller']['message_id'],
            node_id=node.identifier,
            static=static_player,
            skin=self.bot.check_skin(skin),
            custom_skin_data=global_data["custom_skins"],
            custom_skin_static_data=global_data["custom_skins_static"],
            skin_static=self.bot.check_static_skin(static_skin),
            extra_hints=self.extra_hints,
            restrict_mode=guild_data['enable_restrict_mode'],
            listen_along_invite=invite,
            volume=int(guild_data['default_player_volume']),
            autoplay=guild_data["autoplay"]
        )

        player.message = message

        channel = ctx.author.voice.channel

        await player.connect(channel.id)

        self.bot.loop.create_task(ctx.message.add_reaction("👍"))

        while not ctx.guild.me.voice:
            await asyncio.sleep(1)

        if isinstance(channel, disnake.StageChannel):

            stage_perms = channel.permissions_for(ctx.guild.me)
            if stage_perms.manage_permissions:
                await ctx.guild.me.edit(suppress=False)

            await asyncio.sleep(1.5)

        await player.process_next()

    async def cog_check(self, ctx: CustomContext) -> bool:
        return await check_requester_channel(ctx)

    async def cog_load(self) -> None:
        self.owner_view = PanelView(self.bot)

    async def download_lavalink_serverlist(self):
        async with ClientSession() as session:
            async with session.get(self.bot.config["LAVALINK_SERVER_LIST"]) as r:
                ini_file = await r.read()
                with open("lavalink.ini", "wb") as f:
                    f.write(ini_file)


def setup(bot: BotCore):
    bot.add_cog(Owner(bot))
