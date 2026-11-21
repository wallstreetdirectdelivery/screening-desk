# Alert-closing system

```
system/run <institution folder> <output file>
```

Reads one institution folder and writes one JSON line per case in its `cases.jsonl`. It works from any working directory, uses only the Python 3 standard library (tested on 3.9 and 3.14) and makes no network calls.

## How it decides

The system works the way an analyst does: it reads what is on file, checks each document against the transactions, and closes only when every alert in the case is explained. It is a fixed set of checks with no model and no randomness.

**1. Read the case.** For each case it loads the customer, the alerts with the transactions they fired on, the full transaction history, earlier reviews, the internal list entry behind any list match, and every document on file. Documents are parsed into labelled fields, tables and free text.

**2. Read the policy.** Settings that differ between institutions are read from the `POLICY.md` in the folder it is given, not written into the code: the higher-risk countries, whether the rapid-movement rule counts only money from third parties, whether a payment over a threshold needs an invoice before release and what that threshold is, and the multiple at which own-account transfers need an updated profile.

**3. Look for explanations.** Each check compares a document with the transactions and records which transactions it accounts for. A document only counts when its details match: the amount to the penny, the other party's name, and the account number, invoice number or date where the document gives one.

| Check | What has to match | Reason code |
|---|---|---|
| Own account | A statement names the customer as holder of the account on the other side, and shows the transfer | `OWN_ACCOUNT` |
| One-off event | A solicitor's, insurer's, settlement, completion or deposit letter names the payer and the amount | `DOCUMENTED_EVENT` |
| Cash sale | A signed receipt shows the customer sold an item for the cash amount paid in | `DOCUMENTED_EVENT` |
| Gift or invoice finance | A gift letter or remittance advice from the payer for the amount received | `THIRD_PARTY_DOCUMENTED` |
| Invoice, contract, payment file | Payee and amount match, line by line for schedules and payment files | `INVOICE_MATCHES` |
| Business cash records | Every flagged cash deposit matches a line in the takings or collection records on amount and date | `BUSINESS_RECORDS_RECONCILE` |
| Group booking | The payers are on the booking's traveller list at the listed price, and the money went to the firm on the booking | `COLLECTION_DOCUMENTED` |
| Refund | The money matches a refund notice, and the original payment to the same party is on the account | `REFUND_OF_EARLIER_PAYMENT` |
| Group company | A group structure chart links the payer and the customer through ownership | `GROUP_COMPANY` |
| Returning customer | The customer was re-verified before a dormant account was used, and the money itself is explained | `RETURNING_CUSTOMER` |
| List match, different party | A register extract or letter shows a different date of birth, or different people at a different address | `LIST_MATCH_DIFFERENT_PARTY` |
| Updated profile | Own-account transfers at or above the policy multiple, where a later document on file already gives an updated expected income or volume that the activity sits inside | `WITHIN_PROFILE` |

**4. Look for signs of crime.** These force an escalation whatever else is on file.

| Sign | Reason code |
|---|---|
| The listed person has the same date of birth, or still controlled the payee on the payment date | `ESC_LISTED_PARTY` |
| Another bank reports its customer was scammed into paying the account; or unrelated payers' money is forwarded at once from a device shared with other customers; or a payment file shows payees sharing a few addresses, funded by an unrelated company | `ESC_MULE_ACTIVITY` |
| A staff note records the customer asking about staying under a limit, with transactions just under it | `ESC_STRUCTURING` |
| A staff note records a routing the client could not explain, or money fans out to several people in a higher-risk country | `ESC_HIGH_RISK_CORRIDOR` |
| Money arrives from a company the register shows was dissolved; or cash is paid in and sent on at once | `ESC_UNEXPLAINED_FUNDS` |
| Money leaves at once to companies the register shows are owned by the customer's own director | `ESC_PASS_THROUGH` |
| An invoice on file differs from the payment that quotes it | `ESC_INVOICE_MISMATCH` |
| Third parties in a higher-risk country fund an investment account | `ESC_THIRD_PARTY_FUNDING` |
| An outsider pays in, fund units are bought and quickly sold, and the proceeds go to a different outsider | `ESC_EARLY_REDEMPTION` |
| The account's device is shared with other customers or logs in from an undeclared country, or the customer says someone else uses the account, and the alerted activity is otherwise unexplained | `ESC_ACCOUNT_MISUSE` |

These rest on structure: who paid whom, which documents exist and whether their figures agree. What a customer said in a chat is quoted in the reason, but it only counts when the money moved the way the words describe, and ignoring chat wording altogether changes no verdict on the development cases.

A document that contradicts the activity does not explain it. An invoice paid to a different account from the one printed on it, a schedule or letter dated after the money moved, a register extract showing the same address as the list entry, and a figure in a document other than the amount it says was paid all leave the alert unexplained.

**5. Decide.**

1. Any sign of crime: **ESCALATE**.
2. Otherwise test each alert. An alert is explained when what remains unexplained would not have fired the rule, for example when receipts other than the documented ones are back inside the profile limit.
3. Every alert explained: **CLOSE**. There are two exceptions taken from the policies, and both give an **RFI**: own-account transfers at or above the policy multiple (`RFI_UPDATED_PROFILE`), and a held payment at or over the invoice threshold with no matching invoice or contract.
4. Anything else: **RFI**, naming the one document to ask for. The request is chosen from what the unexplained transactions look like, for example a statement for an own-name or joint account, a gift letter, a completion statement, a booking confirmation or a payment file. A trading customer's unexplained takings or receipts get `RFI_BUSINESS_RECORDS`, asking for the takings, collection or sales records behind them.

The system never closes a case by default. If a case cannot be screened in full (an unreadable file, or alert rows missing for the case) it is escalated, one bad file or line does not stop the other cases, and every case in `cases.jsonl` always gets a line.

**6. Write the reason.** The reason is assembled from the facts the checks relied on: document IDs and dates, names, amounts, account numbers, references and transaction IDs. `evidence` lists the deciding documents and list entries first, then the transactions.

## Files

| File | What it does |
|---|---|
| `run` | Entry point |
| `tmlib/data.py` | Loads the folder, parses documents, reads the policy settings |
| `tmlib/detect.py` | The explanation and suspicion checks, and the wording of requests for information |
| `tmlib/engine.py` | Tests whether each alert is explained and turns the findings into a verdict and reason |
| `tmlib/util.py` | Money, date, name and account matching |

## Model

None. No language model or other external service is called, and `OPENROUTER_API_KEY` is not used.

## Time and cost

Measured from a fresh clone on a laptop (Apple M4 Pro): 1,000 cases take between 0.27 and 0.97 seconds depending on the institution, all 4,000 development cases take 2.6 seconds, and a run of 200 cases, measured directly, takes 0.33 seconds. Peak memory is about 200 MB. The cost is nothing.

## Reproducibility

The same input always gives the same output. Five runs per institution with different Python hash seeds, and a run under a different Python version, produced byte-identical files.

## Built for unseen cases

Nothing from the development cases is in the code: no case IDs, customer or company names, amounts, document IDs or verdicts. Every check works from the document types, field labels and policy text that the unseen folders share.
