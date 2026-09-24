# Fraud policy v1.0 and known patterns (bank documents)

## Pattern 1: card testing
A stolen card number is checked before use: three or more tiny online authorizations, often under $5, then a larger purchase. Confirmed by the sequence itself. Policy R5.

## Pattern 2: card-not-present fraud
The number is used online without the card. Amounts and products that do not fit the cardholder's history, often in a burst of two to four within 48 hours. One unusual online purchase on its own is ambiguous: verify. Policy R1 to R4.

## Pattern 3: card-not-present fraud from a new device
As pattern 2, with the identity record marking the device as New for this account, sometimes behind a proxy. Stronger than pattern 2, still not proof: people buy new phones.

## Pattern 4: out-of-region use
Card-present purchases in a billing region the cardholder has no history in, while normal activity continues at home. Several days of purchases in one new region is a trip, not a clone. Policy R2, R3.

## Pattern 5: account takeover
Mixed-channel activity inconsistent with the cardholder, often with device and match-flag anomalies, pointing to stolen credentials rather than a stolen number.

## R1 verify before block on a weak signal
If the case rests on a single signal, including a risk score alone, and assessed fraud probability is below 0.70, recommend VERIFY_WITH_CUSTOMER or STEP_UP_AUTH before any block. Blocking a legitimate customer on one signal is a policy breach.

## R2 customer denies the transaction
Recommend BLOCK_CARD and CREATE_CASE. Add FILE_REPORT if exposure exceeds $1,000 or the case connects to a shared device profile or another card's fraud.

## R3 customer confirms the transaction
Recommend CLOSE_NO_FRAUD and note the confirmation in the case file.

## R4 no reply within 24 hours
Recommend MONITOR_CARD and DECLINE_TRANSACTION for pending authorizations. Escalate if exposure exceeds $500.

## R5 card testing
Three or more small online authorizations on one card within an hour followed by a larger purchase: DECLINE_TRANSACTION and STEP_UP_AUTH. If a purchase over $100 has already cleared, BLOCK_CARD.

## R6 shared origin
When several cards show fraud from the same device profile, billing region or recipient email in one window, name the shared element, CREATE_CASE and FILE_REPORT, and MONITOR_CONNECTED_CARDS for every card that shares it.

## R7 disputed but legitimate
When the customer disputes a charge that matches their own recurring pattern (same merchant, same amount, monthly), CREATE_CASE, VERIFY_WITH_CUSTOMER and WARN_CUSTOMER. Do not block.

## R8 escalate when uncertain and exposed
If the verdict is uncertain and exposure exceeds $500, or the evidence conflicts, ESCALATE_TO_ANALYST.

## R9 undocumented patterns
Activity that fits none of the known patterns but shows coordinated or repeated abuse across customers: CREATE_CASE, FILE_REPORT and ESCALATE_TO_ANALYST, and describe the pattern in plain words.

## R10 block all cards
Never BLOCK_ALL_CARDS unless at least two of the customer's cards show confirmed fraud or the customer's credentials are confirmed compromised.

## 3a a case is not a report
Open a case whenever fraud probability reaches 0.30, whenever evidence is requested, or whenever a customer disputes a charge. File a suspicious activity report when fraud is confirmed or strongly suspected and exposure exceeds $1,000, or the activity connects to a shared device profile, shared region cluster or another customer's fraud, or the pattern is coordinated or undocumented.

## 6 stopping
Stop when fraud probability is at or above 0.85 or at or below 0.15 with at least two independent pieces of evidence, when a verification response settles the question, or when further steps are unlikely to change the decision.
