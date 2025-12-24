import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import functools
import os
from discord.ui import View, Button, button

# ---------------------------
# Bot Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
TREE = bot.tree

# ---------------------------
# yt-dlp config
# ---------------------------
ytdl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
}
ytdl = yt_dlp.YoutubeDL(ytdl_opts)

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = '-vn -af "volume={volume}"'

# ---------------------------
# Guild Music State
# ---------------------------
class GuildMusic:
    def __init__(self):
        self.queue = []
        self.history = []
        self.voice: discord.VoiceClient | None = None
        self.current = None
        self.playing = False
        self.loop = False
        self.volume = 1.0
        self.panel_message: discord.Message | None = None

music_data: dict[int, GuildMusic] = {}

def get_guild_music(gid: int) -> GuildMusic:
    if gid not in music_data:
        music_data[gid] = GuildMusic()
    return music_data[gid]

# ---------------------------
# yt-dlp helper
# ---------------------------
async def extract_info(query: str):
    loop = asyncio.get_event_loop()
    func = functools.partial(ytdl.extract_info, query, download=False)
    info = await loop.run_in_executor(None, func)
    if "entries" in info:
        info = info["entries"][0]
    return info

# ---------------------------
# Voice State Cleanup (ADMIN KICK FIX)
# ---------------------------
@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id:
        return

    if before.channel and not after.channel:
        gm = music_data.get(member.guild.id)
        if gm:
            gm.voice = None
            gm.queue.clear()
            gm.history.clear()
            gm.current = None
            gm.playing = False
            gm.panel_message = None
            print(f"[Voice] Disconnected from {member.guild.name}")

# ---------------------------
# Music Panel
# ---------------------------
class MusicPanel(View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @button(label="⏭ Skip", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        await interaction.response.defer()
        if gm.voice and gm.voice.is_playing():
            gm.voice.stop()
            await interaction.followup.send("⏭ Skipped.")
        else:
            await interaction.followup.send("❌ Nothing playing.")

    @button(label="⏸ Pause/Resume", style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        await interaction.response.defer()
        if not gm.voice:
            return await interaction.followup.send("❌ Not connected.")
        if gm.voice.is_playing():
            gm.voice.pause()
            await interaction.followup.send("⏸ Paused.")
        elif gm.voice.is_paused():
            gm.voice.resume()
            await interaction.followup.send("▶ Resumed.")

# ---------------------------
# Panel Update (ONLY CALLED WHEN SONG STARTS)
# ---------------------------
async def update_panel(guild_id: int, channel: discord.abc.Messageable):
    gm = get_guild_music(guild_id)

    embed = discord.Embed(title="🎶 Now Playing", color=0x5865F2)

    if gm.current:
        embed.add_field(name="Track", value=gm.current["title"], inline=False)
        embed.add_field(name="Volume", value=f"{gm.volume:.1f}x", inline=True)
        embed.add_field(name="Loop", value=str(gm.loop), inline=True)

    if gm.queue:
        q = "\n".join(f"{i}. {s['title']}" for i, s in enumerate(gm.queue[:6], 1))
        embed.add_field(name="Up Next", value=q, inline=False)
    else:
        embed.add_field(name="Up Next", value="(empty)", inline=False)

    view = MusicPanel(guild_id)

    if gm.panel_message:
        await gm.panel_message.edit(embed=embed, view=view)
    else:
        gm.panel_message = await channel.send(embed=embed, view=view)

# ---------------------------
# Playback Logic
# ---------------------------
async def play_next(interaction, gm: GuildMusic):
    if not gm.queue:
        gm.playing = False
        gm.current = None

        if gm.voice and gm.voice.is_connected():
            await gm.voice.disconnect()
            gm.voice = None

        return

    entry = gm.queue.pop(0)
    info = await extract_info(entry["webpage_url"])

    gm.current = {
        "title": info["title"],
        "url": info["url"]
    }

    ffmpeg_opts = {
        "before_options": FFMPEG_BEFORE,
        "options": FFMPEG_OPTS.format(volume=gm.volume)
    }

    def after(err):
        if err:
            print("Playback error:", err)
        asyncio.run_coroutine_threadsafe(play_next(None, gm), bot.loop)

    if not gm.voice or not gm.voice.is_connected():
        return

    source = discord.FFmpegOpusAudio(
        gm.current["url"],
        **ffmpeg_opts
    )

    gm.voice.play(source, after=after)
    gm.playing = True

    # 🔑 PANEL UPDATES ONLY WHEN AUDIO STARTS
    channel = interaction.channel if interaction else gm.panel_message.channel
    await update_panel(gm.voice.guild.id, channel)

    if interaction:
        await interaction.followup.send(f"▶ **Now playing:** {gm.current['title']}")

# ---------------------------
# Slash Commands
# ---------------------------
@TREE.command(name="play", description="Play a song or search YouTube")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    gm = get_guild_music(interaction.guild_id)

    if not interaction.user.voice:
        return await interaction.followup.send("❌ Join a voice channel first.")

    channel = interaction.user.voice.channel

    if gm.voice is None or not gm.voice.is_connected():
        gm.voice = await channel.connect()

    if not query.startswith("http"):
        info = await extract_info(f"ytsearch:{query}")
        url = info["webpage_url"]
        title = info["title"]
    else:
        info = await extract_info(query)
        url = query
        title = info["title"]

    gm.queue.append({"webpage_url": url, "title": title})
    await interaction.followup.send(f"🎵 Added **{title}** to queue.")

    if not gm.playing:
        await play_next(interaction, gm)

@TREE.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    gm = get_guild_music(interaction.guild_id)
    await interaction.response.defer()

    if gm.voice and gm.voice.is_playing():
        gm.voice.stop()
        await interaction.followup.send("⏭ Skipped.")
    else:
        await interaction.followup.send("❌ Nothing to skip.")

# ---------------------------
# Ready
# ---------------------------
@bot.event
async def on_ready():
    await TREE.sync()
    print(f"Bot ready as {bot.user}")

# ---------------------------
# Run
# ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
bot.run(TOKEN)

