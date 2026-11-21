# Two-minute live demo

About 270 spoken words, which is two minutes at a calm pace. **Bold** lines are what to do on screen; everything else is what to say.

## Before you go on

1. In a terminal: `python3 ui/server.py`
2. Open three browser tabs, in this order:
   - <http://127.0.0.1:8765/#bank/BK-CS125943> (the £1,800 case)
   - <http://127.0.0.1:8765/#investment/IV-CS103503> (the gift that closes)
   - <http://127.0.0.1:8765/bot?step&demo> (the chat bot; press Space to move to the next message)
3. In a fourth tab, open a terminal in the repo, ready to run `sh eval/check.sh`. Optional; only if time allows.
4. Browser zoom at 110 to 125% so the back of the room can read it.

## Script

### 0:00 The question (15 s)

**Tab 1, at the top of the case.**

> Here's an alert. A customer paid £1,800 to a training company, and there's an invoice on file that matches it to the penny. Most tools would close this. Should it be closed?

### 0:15 The screening steps (50 s)

**Click "Replay screening". Scroll as the steps appear.**

> Screening desk reads an alert the way a good analyst does, and shows every step.

**Step 1 and 2.**

> It reads the customer and what they said to expect, then the alert: the payee matched our internal list.

**Step 4.**

> It matches documents to the money. A document only counts if amount, party and date all agree. Here the invoice does explain the payment.

**Step 5. Click the `PSC-5155` chip; the control register opens on the right.**

> But it also looks for signs of crime. The company's former director is on our list for an open investigation, and the control register shows he still owned 60% on the day the money moved.

**Step 7, then scroll to the red warning.**

> So it escalates. The invoice doesn't clear it. And because there's suspicion, it drafts the referral to the MLRO and never contacts the customer. That would be tipping off.

### 1:05 Most alerts close (15 s)

**Tab 2. Click "Show all", scroll to the CLOSE stamp.**

> Most alerts do close. Here a gift letter matches the deposit on donor, amount and date, so it's closed, citing the letter.

### 1:20 Inside a chat bot (25 s)

**Tab 3. Press Space between messages.**

> The same desk sits behind an MCP connector, so an agent like Grok can use it as tools. Here's how that looks inside a chat bot. A new alert lands; it escalates, drafts the referral. The analyst asks "why not just close it?", and it answers from the case file. The rules decide; the bot explains. It can't change a verdict.

### 1:45 Close (15 s)

**Back to Tab 1, or stay on the bot.**

> Twenty-eight checks, run as code: four thousand alerts in under three seconds, identical on every run. Wrong evidence never closes a case; anything it can't explain goes to a person. Thank you.

## If a judge asks

- **Is that Grok in the chat?** It's a mock-up of the chat front end. The data in it comes live through our MCP connector. The connector is built and ready for Grok: the "Connect an agent" tab has the exact request.
- **Where's the language model?** Around the decision, not in it. Verdicts come from fixed checks, so they're reproducible and auditable. Models review escalations, answer questions through the tools and draft the paperwork. With a key set, the Assistant tab and the AI supervisor run live.
- **How do you know it's right without answers?**
  - We hand-labelled 160 cases covering all 128 scenario types, and agree on all of them, with no wrongful closes.
  - Every institution comes out at exactly 500 closed, 250 RFI and 250 escalated, which we didn't aim for.
  - `sh eval/check.sh` runs every check in 20 seconds.
- **What about a document it's never seen?** It's ignored, so the case becomes a request for information. It can't cause a close.
- **What if a document contradicts the payment?** It doesn't count. An invoice paid to a different account, or a letter dated after the money moved, leaves the alert open. We test 21 cases like that.
