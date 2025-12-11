import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import functools
import os
from discord.ui import View, Button, button

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
TREE = bot.tree

# ---------------------------
# yt-dlp defaults
# ---------------------------
ytdl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "nocheckcertificate": True,
}
ytdl = yt_dlp.YoutubeDL(ytdl_opts)

FFMPEG_BASE_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS_TEMPLATE = '-vn -af "volume={volume}"'

# ---------------------------
# Guild State
# ---------------------------
class GuildMusic:
    def __init__(self):
        self.queue = []
        self.playing = False
        self.voice: discord.VoiceClient | None = None
        self.current: dict | None = None
        self.history = []
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
async def extract_info(url_or_search: str):
    loop = asyncio.get_event_loop()
    func = functools.partial(ytdl.extract_info, url_or_search, download=False)
    try:
        info = await loop.run_in_executor(None, func)
    except Exception as e:
        raise RuntimeError(f"yt-dlp failed: {e}")
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    return info


# ---------------------------
# Music Panel
# ---------------------------
class MusicPanel(View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @button(label="Volume -", style=discord.ButtonStyle.secondary, emoji="🔉", custom_id="vol_down")
    async def vol_down(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        gm.volume = max(0.1, round(gm.volume - 0.1, 2))

        await interaction.response.defer(ephemeral=True)

        if gm.current and gm.voice and gm.voice.is_playing():
            await restart_current(interaction, gm)
            await interaction.followup.send(f"🔉 Volume set to {gm.volume:.1f}", ephemeral=True)
        else:
            await interaction.followup.send(f"🔉 Volume set to {gm.volume:.1f}", ephemeral=True)

        await update_panel(interaction.guild_id, interaction)

    @button(label="Volume +", style=discord.ButtonStyle.secondary, emoji="🔊", custom_id="vol_up")
    async def vol_up(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        gm.volume = min(2.0, round(gm.volume + 0.1, 2))

        await interaction.response.defer(ephemeral=True)

        if gm.current and gm.voice and gm.voice.is_playing():
            await restart_current(interaction, gm)
            await interaction.followup.send(f"🔊 Volume set to {gm.volume:.1f}", ephemeral=True)
        else:
            await interaction.followup.send(f"🔊 Volume set to {gm.volume:.1f}", ephemeral=True)

        await update_panel(interaction.guild_id, interaction)

    @button(label="Back", style=discord.ButtonStyle.primary, emoji="⏮", custom_id="back")
    async def back(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        await interaction.response.defer()

        if gm.history:
            if gm.current:
                gm.queue.insert(0, {"webpage_url": gm.current["webpage_url"], "title": gm.current["title"]})
            prev = gm.history.pop()
            gm.queue.insert(0, prev)
            if gm.voice and gm.voice.is_playing():
                gm.voice.stop()
            await interaction.followup.send(f"⏮ Playing previous: **{prev['title']}**")
        elif gm.current:
            if gm.voice and gm.voice.is_playing():
                await restart_current(interaction, gm)
                await interaction.followup.send("🔁 Restarted current track.")
        else:
            await interaction.followup.send("❌ Nothing to go back to.")

        await update_panel(interaction.guild_id, interaction)

    @button(label="Pause/Resume", style=discord.ButtonStyle.primary, emoji="⏸️", custom_id="pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        await interaction.response.defer()

        if gm.voice is None:
            return await interaction.followup.send("❌ Bot not connected.")

        if gm.voice.is_playing():
            gm.voice.pause()
            await interaction.followup.send("⏸ Paused.")
        elif gm.voice.is_paused():
            gm.voice.resume()
            await interaction.followup.send("▶ Resumed.")
        else:
            await interaction.followup.send("❌ Nothing is playing.")

        await update_panel(interaction.guild_id, interaction)

    @button(label="Skip", style=discord.ButtonStyle.danger, emoji="⏭️", custom_id="skip")
    async def skip(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        await interaction.response.defer()

        if gm.voice and gm.voice.is_playing():
            gm.voice.stop()
            await interaction.followup.send("⏭ Skipped.")
        else:
            if gm.queue:
                await play_next(interaction, gm)
                await interaction.followup.send("⏭ Started next track.")
            else:
                await interaction.followup.send("❌ Nothing to skip.")

        await update_panel(interaction.guild_id, interaction)

    @button(label="Loop", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="loop")
    async def loop(self, interaction: discord.Interaction, b: Button):
        gm = get_guild_music(interaction.guild_id)
        gm.loop = not gm.loop
        await interaction.response.send_message(f"🔁 Loop is now {'ON' if gm.loop else 'OFF'}.", ephemeral=True)
        await update_panel(interaction.guild_id, interaction)


# ---------------------------
# Update Panel (Fixed)
# ---------------------------
async def update_panel(guild_id: int, interaction: discord.Interaction | None):
    gm = get_guild_music(guild_id)

    embed = discord.Embed(title="🎶 Music Panel", color=0x5865F2)

    if gm.current:
        embed.add_field(name="Now Playing", value=f"**{gm.current['title']}**", inline=False)
        embed.add_field(name="Requested By", value=gm.current.get("requested_by", "Unknown"), inline=True)
        embed.add_field(name="Loop", value=str(gm.loop), inline=True)
        embed.add_field(name="Volume", value=f"{gm.volume:.1f}x", inline=True)
    else:
        embed.description = "No track playing."

    if gm.queue:
        qtext = "\n".join([f"{i}. {s['title']}" for i, s in enumerate(gm.queue[:6], 1)])
        embed.add_field(name="Queue", value=qtext, inline=False)
    else:
        embed.add_field(name="Queue", value="(empty)", inline=False)

    view = MusicPanel(guild_id)

    # fallback logic
    if interaction:
        channel = interaction.channel
    elif gm.panel_message:
        channel = gm.panel_message.channel
    else:
        return  # nowhere to post

    try:
        if gm.panel_message:
            await gm.panel_message.edit(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed, view=view)
            gm.panel_message = msg
    except Exception as e:
        print("Failed updating panel:", e)


# ---------------------------
# Play Logic
# ---------------------------
async def restart_current(interaction: discord.Interaction, gm: GuildMusic):
    if not gm.current:
        return
    gm.queue.insert(0, {"webpage_url": gm.current["webpage_url"], "title": gm.current["title"]})
    if gm.voice and gm.voice.is_playing():
        gm.voice.stop()
    else:
        await play_next(interaction, gm)

async def play_next(interaction: discord.Interaction | None, gm: GuildMusic):

    if not gm.queue:
        gm.playing = False
        gm.current = None
        await update_panel(gm.voice.guild.id if gm.voice else (interaction.guild.id if interaction else 0), interaction)
        return

    entry = gm.queue.pop(0)
    webpage_url = entry["webpage_url"]
    title = entry["title"]

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(webpage_url, download=False))
    except Exception as e:
        print("yt-dlp extract failed:", e)
        return await play_next(interaction, gm)

    if "entries" in info:
        if info["entries"]:
            info = info["entries"][0]
        else:
            return await play_next(interaction, gm)

    audio_url = info.get("url")
    if not audio_url:
        return await play_next(interaction, gm)

    gm.current = {
        "webpage_url": webpage_url,
        "title": title,
        "audio_url": audio_url,
        "requested_by": entry.get("requested_by", "Unknown")
    }
    gm.history.append({"webpage_url": webpage_url, "title": title})

    ffmpeg_options = {
        "before_options": FFMPEG_BASE_BEFORE,
        "options": FFMPEG_OPTIONS_TEMPLATE.format(volume=gm.volume)
    }

    def after_play(err):
        if err:
            print("Playback error:", err)
        if gm.loop and gm.current:
            gm.queue.insert(0, {"webpage_url": gm.current["webpage_url"], "title": gm.current["title"]})
        asyncio.run_coroutine_threadsafe(play_next(None, gm), bot.loop)

    try:
        source = discord.FFmpegOpusAudio(
            gm.current["audio_url"],
            before_options=ffmpeg_options["before_options"],
            options=ffmpeg_options["options"]
        )
        if not gm.voice:
            gm.playing = False
            return
        gm.voice.play(source, after=after_play)
        gm.playing = True
    except Exception as e:
        print("Failed to play source:", e)
        gm.playing = False
        return await play_next(interaction, gm)

    if interaction:
        try:
            await interaction.followup.send(f"▶ **Now playing:** {title}")
        except:
            pass

    await update_panel(
        gm.voice.guild.id if gm.voice else (interaction.guild.id if interaction else 0),
        interaction
    )


# ---------------------------
# Slash Commands
# ---------------------------
@TREE.command(name="play", description="Play a song by URL or search.")
@app_commands.describe(query="YouTube / SoundCloud link OR search text")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    gm = get_guild_music(interaction.guild_id)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ You must be in a voice channel.")

    if not gm.voice:
        gm.voice = await interaction.user.voice.channel.connect()
        await asyncio.sleep(0.4)

    if not (query.startswith("http://") or query.startswith("https://")):
        info = await extract_info(f"ytsearch:{query}")
        webpage_url = info.get("webpage_url") or info.get("id")
        title = info.get("title", "Unknown Title")
    else:
        info = await extract_info(query)
        webpage_url = info.get("webpage_url") or query
        title = info.get("title", "Unknown Title")

    gm.queue.append({"webpage_url": webpage_url, "title": title, "requested_by": interaction.user.display_name})

    await interaction.followup.send(f"🎵 Added to queue: **{title}**")
    await update_panel(interaction.guild_id, interaction)

    if not gm.playing:
        await play_next(interaction, gm)


@TREE.command(name="skip", description="Skip to next song.")
async def skip_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    gm = get_guild_music(interaction.guild_id)

    if gm.voice and gm.voice.is_playing():
        gm.voice.stop()
        await interaction.followup.send("⏭ Skipped.")
    else:
        if gm.queue:
            await play_next(interaction, gm)
            await interaction.followup.send("⏭ Started next track.")
        else:
            await interaction.followup.send("❌ Nothing to skip.")

    await update_panel(interaction.guild_id, interaction)


@TREE.command(name="stop", description="Stop music and clear queue.")
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    gm = get_guild_music(interaction.guild_id)
    gm.queue.clear()
    gm.playing = False

    if gm.voice and gm.voice.is_playing():
        gm.voice.stop()

    await interaction.followup.send("⛔ Stopped and cleared queue.")
    await update_panel(interaction.guild_id, interaction)


@TREE.command(name="pause", description="Pause current song.")
async def pause_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    gm = get_guild_music(interaction.guild_id)

    if gm.voice and gm.voice.is_playing():
        gm.voice.pause()
        await interaction.followup.send("⏸ Paused.")
    else:
        await interaction.followup.send("❌ Nothing is playing.")

    await update_panel(interaction.guild_id, interaction)


@TREE.command(name="resume", description="Resume paused song.")
async def resume_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    gm = get_guild_music(interaction.guild_id)

    if gm.voice and gm.voice.is_paused():
        gm.voice.resume()
        await interaction.followup.send("▶ Resumed.")
    else:
        await interaction.followup.send("❌ Nothing is paused.")

    await update_panel(interaction.guild_id, interaction)


@TREE.command(name="loop", description="Toggle loop.")
async def loop_cmd(interaction: discord.Interaction):
    gm = get_guild_music(interaction.guild_id)
    gm.loop = not gm.loop
    await interaction.response.send_message(f"🔁 Loop is now {'ON' if gm.loop else 'OFF'}.")
    await update_panel(interaction.guild_id, interaction)


# ---------------------------
# Bot Ready
# ---------------------------
@bot.event
async def on_ready():
    try:
        await TREE.sync()
    except Exception as e:
        print("Failed to sync commands:", e)
    print(f"Bot ready as {bot.user} (ID: {bot.user.id})")


# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN") or "YOUR_TOKEN"
    bot.run(TOKEN)
