"""Build the derived transaction table and the closed-case fraud model.

Inputs (DATA_DIR, default ./data): transactions.csv, identity.csv,
closed_cases_history.csv, case_pack.csv.
Outputs (DATA_DIR/derived): txn.parquet, closed_cases.parquet, model.txt.

Card IDs: transactions.csv carries customer_id but no card_id. Card IDs are
recovered from the closed cases and the case pack, which name the card for
each listed transaction. A customer's card is keyed by the card attribute
tuple (card2..card6); a tuple seen on a labeled transaction takes that label,
otherwise the customer's most frequent labeled card, otherwise <customer>-K1.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

DATA = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
OUT = DATA / "derived"

CAT = ["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "M1", "M2", "M3", "M4", "M5",
       "M6", "M7", "M8", "M9", "id_12", "id_15", "id_16", "id_23", "id_27", "id_28", "id_29", "id_30",
       "id_31", "id_33", "id_34", "id_35", "id_36", "id_37", "id_38", "DeviceType", "DeviceInfo",
       "channel", "device_profile"]


def device_profile(idn: pd.DataFrame) -> pd.Series:
    def s(x):
        return x.fillna("?").astype(str)
    return s(idn.DeviceInfo) + " | " + s(idn.id_30) + " | " + s(idn.id_31) + " | " + s(idn.id_33)


def device_id(profile: str) -> str:
    return "DP" + hashlib.sha1(profile.encode()).hexdigest()[:10]


def load_raw() -> tuple[pd.DataFrame, pd.DataFrame]:
    cols = pd.read_csv(DATA / "transactions.csv", nrows=0).columns
    vcols = [c for c in cols if c.startswith("V")]
    parts = [ch for ch in pd.read_csv(DATA / "transactions.csv", chunksize=100_000, low_memory=False,
                                      dtype={c: "float32" for c in vcols})]
    tx = pd.concat(parts, ignore_index=True)
    idn = pd.read_csv(DATA / "identity.csv")
    idn["device_profile"] = device_profile(idn)
    return tx.merge(idn, on="TransactionID", how="left"), idn


def assign_cards(tx: pd.DataFrame, cc: pd.DataFrame, cp: pd.DataFrame) -> pd.Series:
    key = tx.card2.astype(str)
    for c in ["card3", "card4", "card5", "card6"]:
        key = key + "|" + tx[c].astype(str)
    tx["_ckey"] = key
    lab = [(int(t), r.card_id) for r in cc.itertuples() for t in str(r.txn_ids).split("|") if t != "nan"]
    lab += [(int(r.flagged_txn_id), r.card_id) for r in cp.itertuples()]
    lab = pd.DataFrame(lab, columns=["TransactionID", "card_id"]).drop_duplicates("TransactionID")
    j = lab.merge(tx[["TransactionID", "customer_id", "_ckey"]], on="TransactionID")
    by_key = j.groupby(["customer_id", "_ckey"]).card_id.agg(lambda s: s.value_counts().index[0])
    by_cust = j.groupby("customer_id").card_id.agg(lambda s: s.value_counts().index[0])
    card = pd.Series(list(zip(tx.customer_id, tx._ckey)), index=tx.index).map(by_key)
    card = card.fillna(tx.customer_id.map(by_cust)).fillna(tx.customer_id + "-K1")
    exact = tx.TransactionID.map(lab.set_index("TransactionID").card_id)
    return exact.fillna(card)


def train_model(tx: pd.DataFrame, cc: pd.DataFrame) -> pd.Series:
    X = tx  # mutated in place to save memory; callers snapshot raw columns first
    for c in CAT:
        X[c + "_fe"] = X[c].map(X[c].value_counts(dropna=False)).astype("float32")
        X[c] = X[c].astype("category").cat.codes
    X["amt_cents"] = (X.TransactionAmt * 100 % 100).astype("float32")
    X["hour"] = X.ts.dt.hour
    g = X.groupby("uid").TransactionAmt
    X["uid_cnt"] = g.transform("size")
    X["uid_amt_mean"] = g.transform("mean")
    X["amt_ratio"] = X.TransactionAmt / X.uid_amt_mean
    X["uid_dev_n"] = X.groupby("uid").device_profile.transform("nunique")
    pos = {int(t) for r in cc[cc.outcome == "confirmed_fraud"].itertuples() for t in str(r.txn_ids).split("|")}
    neg = {int(t) for r in cc[cc.outcome == "cleared"].itertuples() for t in str(r.txn_ids).split("|")}
    hist = X[X.ts < "2016-11-01"]
    rest = hist[~hist.TransactionID.isin(pos | neg)].sample(120_000, random_state=0)
    tr = pd.concat([hist[hist.TransactionID.isin(pos)].assign(y=1),
                    hist[hist.TransactionID.isin(neg)].assign(y=0), rest.assign(y=0)])
    drop = {"TransactionID", "ts", "customer_id", "uid", "TransactionDT", "y", "day", "card1", "card_id",
            "_ckey", "device_id"}
    feats = [c for c in tr.columns if c not in drop and tr[c].dtype != object]
    params = dict(objective="binary", learning_rate=0.05, num_leaves=63, min_data_in_leaf=40,
                  feature_fraction=0.5, bagging_fraction=0.8, bagging_freq=1, verbose=-1, seed=7)
    model = lgb.train(params, lgb.Dataset(tr[feats].astype("float32"), tr.y), 600)
    model.save_model(str(OUT / "model.txt"))
    preds = [model.predict(X[feats].iloc[i:i + 100_000].astype("float32")) for i in range(0, len(X), 100_000)]
    return pd.Series(np.concatenate(preds), index=X.index)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tx, _ = load_raw()
    cc = pd.read_csv(DATA / "closed_cases_history.csv")
    cp = pd.read_csv(DATA / "case_pack.csv")
    tx["ts"] = pd.to_datetime(tx.ts)
    tx["day"] = tx.TransactionDT // 86400
    tx["uid"] = tx.card1.astype(str) + "_" + tx.addr1.astype(str) + "_" + (tx.day - tx.D1).astype(str)
    tx["card_id"] = assign_cards(tx, cc, cp)
    tx["device_id"] = tx.device_profile.map(lambda p: device_id(p) if isinstance(p, str) else None)
    keep = {"TransactionID": "txn_id", "ts": "ts", "TransactionAmt": "amt", "ProductCD": "product",
            "channel": "channel", "addr1": "addr1", "addr2": "addr2", "dist1": "dist1",
            "P_emaildomain": "p_email", "R_emaildomain": "r_email", "risk_score": "risk_score",
            "id_15": "device_status", "id_23": "proxy", "M4": "m4", "M6": "m6",
            "DeviceType": "device_type", "device_profile": "device_profile", "device_id": "device_id",
            "card_id": "card_id", "customer_id": "customer_id", "uid": "uid", "card4": "network",
            "card6": "card_type", "C1": "c1", "C13": "c13", "C14": "c14", "D1": "d1", "D15": "d15"}
    out = tx[list(keep)].rename(columns=keep).copy()
    out["model_p"] = train_model(tx, cc).values
    out["txn_id"] = out.txn_id.astype(str)
    out["addr1"] = out.addr1.map(lambda v: "" if pd.isna(v) else str(int(v)))
    out.to_parquet(OUT / "txn.parquet", index=False, compression="zstd", compression_level=19)
    cc.to_parquet(OUT / "closed_cases.parquet", index=False)
    print("txn", out.shape, "cards", out.card_id.nunique(), "devices", out.device_id.nunique())


if __name__ == "__main__":
    main()
