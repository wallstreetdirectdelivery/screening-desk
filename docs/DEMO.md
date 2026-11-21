# Demo script

Start the viewer first: `python3 ui/server.py`, then open <http://127.0.0.1:8765>.

The recorded video (`docs/demo.mp4`, 2 min 9 s) follows this storyline. For a live 3-minute demo, use the same order and add the optional parts at the end.

## The storyline

| Time | Beat | On screen |
|---|---|---|
| 0:00 | **The question.** "A £1,800 payment. The invoice matches to the penny. Should this alert be closed?" | Title card |
| 0:07 | **Who answers it.** Screening desk reads each alert the way a good analyst does, and shows its working. | Logo |
| 0:15 | **The seven screening steps, one at a time,** on [BK-CS125943](http://127.0.0.1:8765/#bank/BK-CS125943): read the case, read the alert, read the policy, match the documents to the money (the invoice explains the payment), look for signs of crime (the listed director still owned 60% that day), test each alert, decide: ESCALATE without contacting the customer. | The case page; steps appear as they are narrated |
| 1:02 | **Most alerts do close.** A gift letter matches the deposit on donor, amount and date. 28 checks that run as code: the same answer every time. | [IV-CS103503](http://127.0.0.1:8765/#investment/IV-CS103503) |
| 1:21 | **The same desk inside a chat bot,** through its connector: the overnight queue, a new escalation, and "why not just close it?" answered from the case file. | <http://127.0.0.1:8765/bot> (`?step` advances a scene per Space; `?demo` hides the mock-up label) |
| 1:52 | **Close.** 4,000 alerts in 2.6 seconds. Identical on every run. Nothing closes unless the evidence explains it. | Title card, then the event and track |

## Optional extras for a live demo

### A case that closes

Open [IV-CS103503](http://127.0.0.1:8765/#investment/IV-CS103503) (Investment).

- Two alerts: receipts nine times the declared income, and a payer who is not the customer.
- Step 4: the gift letter is matched to the receipt on donor, amount and date. Click the `GFT-2351` chip to show the letter in the case file.
- Step 6: the arithmetic. £35,228 received, £34,604 explained, £624 left, inside the limit.
- Verdict: CLOSE, with a closing note ready for the record.

### A case that needs one document

Open [EM-CS104805](http://127.0.0.1:8765/#emoney/EM-CS104805) (E-money).

- £13,634 from a retailer, reference "SETTLEMENT AGREEMENT", nothing on file.
- Nothing suspicious, so it is an RFI, and the request names the exact document.
- Show the drafted message: it asks for the letter and says nothing about monitoring.

### The escalation, step by step

Open [BK-CS125943](http://127.0.0.1:8765/#bank/BK-CS125943) (Bank).

- A £1,800 payment for a training course, with an invoice that matches to the penny. A simple matcher would close it.
- But the payee's former director matches the internal list, and the alert says he left the day before the payment.
- Step 5: the control register shows he still owned 60% of the company on the payment date. ESCALATE.
- Show the red warning: do not contact the customer. The MLRO referral is drafted.

Then open [EM-CS102434](http://127.0.0.1:8765/#emoney/EM-CS102434) if there is time: ten strangers pay in, the money goes to a crypto exchange within hours, two banks report scams.

### If a model key is set: rules decide, models assist

Still on [BK-CS125943](http://127.0.0.1:8765/#bank/BK-CS125943):

- Under the verdict, click **Ask the AI supervisor**. A model reviews the escalation as the MLRO's deputy: whether the grounds hold up, what the file does not rule out, and a draft report narrative. It cannot change the verdict.
- Open the **Assistant** tab and click "Check the verdict on BK-CS125943 against the documents in its file." Point at the tool calls appearing: the model is working through the same MCP connector that Grok, Cursor or Claude can use (the **Connect an agent** tab).

### Why it can be trusted

> There is no model at run time. We codified the four policies into 28 checks and they run as code, so the answer is the same every time, costs nothing, and takes 2.6 seconds for 4,000 cases.

In a terminal: `sh eval/check.sh`

- 160 of 160 hand-labelled verdicts agree, with no wrongful closes.
- Byte-identical output on every run.
- 21 tests that change one detail of a real case, such as paying the invoice to a different account, and confirm the system refuses to close it.

> It never closes by default. Anything it cannot explain goes to a person.

## If asked

- **Where is the language model?** Around the decision, not in it. Verdicts come from fixed checks, because reproducibility is a fifth of the score and a requirement in compliance, and the decisions rest on whether amounts, names, accounts and dates agree, which code checks exactly. Models review escalations, answer questions through the MCP tools, and draft the paperwork. The next place for one is reading unfamiliar or messy documents into the same facts.
- **What happens on a document it has never seen?** It is ignored, so the case becomes an RFI. It cannot cause a close.
- **How do you know it is right without answers?** We hand-labelled 160 cases covering all 128 scenario families, and every institution comes out at exactly 500 / 250 / 250, which we did not aim for.
