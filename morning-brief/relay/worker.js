// Free Telegram relay: paste this into a Cloudflare Worker.
// Telegram sends each message here; the Worker checks it is you, then starts the "chat"
// GitHub Action, which writes the answer and sends it back to Telegram (about 1 minute).
//
// Worker variables to set (Settings -> Variables and Secrets, type "Secret"):
//   TELEGRAM_BOT_TOKEN  your BotFather token
//   TELEGRAM_CHAT_ID    your chat id
//   WEBHOOK_SECRET      any random string (letters, numbers, _ or -), also used in setWebhook
//   GITHUB_TOKEN        fine-grained token with "Actions: Read and write" on the morning-brief repo
//   GITHUB_REPO         YOUR_USERNAME/morning-brief

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Morning Brief relay is running.");

    // Only Telegram knows the secret, so random visitors cannot trigger your Actions
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json();
    const msg = update.message;
    if (!msg || !msg.text) return new Response("ok");

    const chatId = String(msg.chat.id);
    const send = (text) =>
      fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text }),
      });

    if (chatId !== String(env.TELEGRAM_CHAT_ID)) {
      await send("This is a private bot.");
      return new Response("ok");
    }

    const gh = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/chat.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "morning-brief-relay",
        },
        body: JSON.stringify({ ref: "main", inputs: { message: msg.text.slice(0, 2000) } }),
      }
    );

    const quiet = /^\/(start|help)\b/.test(msg.text);
    if (!gh.ok) {
      await send(`⚠️ Could not start the job (GitHub ${gh.status}). Check GITHUB_TOKEN and GITHUB_REPO.`);
    } else if (!quiet) {
      await send("⏳ On it, about a minute...");
    }
    return new Response("ok"); // always 200 so Telegram does not resend the message
  },
};
