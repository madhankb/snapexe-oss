# snapexe-oss - 5-Minute Demo Video: Script & Storyboard

**Audience:** Tech Lead / CTO. Lead with business value (risk, time, expertise, lock-in); let the terminal prove the technical depth.
**Format:** Live agent-driven screen recording (Claude Code driving the skill against the real `os-source-*` / `os-dest-*` clusters) + voiceover.
**Runtime target:** 5:00. Narration is ~730 words at a measured ~150 wpm, leaving room for output to land.

## Scene map

| # | Time | Beat | Purpose (judging lens) |
|---|------|------|------------------------|
| 1 | 0:00-0:30 | The problem | Hook - customer pain |
| 2 | 0:30-1:00 | What it is | Innovation + portability |
| 3 | 1:00-1:25 | The source (what's at stake) | Sets up the payoff |
| 4 | 1:25-1:40 | Dest is empty | Establishes the "before" |
| 5 | 1:40-2:35 | Back up (one prompt) | Productivity - the wow |
| 6 | 2:35-2:50 | Status: SUCCESS | Operational honesty |
| 7 | 2:50-3:45 | Restore / migrate | Technical innovation - the hard part |
| 8 | 3:45-4:15 | Prove it's queryable | Utility - real proof |
| 9 | 4:15-4:40 | Teardown (dry-run + confirm) | Operational excellence - safety |
| 10 | 4:40-5:00 | Why it wins | Close + call to action |

---

## Scene 1 - The problem (0:00-0:30)

**On-screen:** Open on a title card ("snapexe-oss - OpenSearch backup, migration & teardown as an agent skill"), then hard-cut to the terminal at the source cluster. Lower-third: *"Data streams + searchable snapshots = the hard part."*

**Narration:**
> "Backing up and migrating a self-managed OpenSearch cluster sounds simple - until you hit data streams and searchable snapshots. Then it's a multi-hour runbook: provision S3, mint IAM users, push keystore keys to every node, juggle superadmin certificates, and remap searchable indices by hand. One wrong step and you've either lost data or left billable resources running. snapexe-oss turns that entire runbook into a single sentence."

---

## Scene 2 - What it is (0:30-1:00)

**On-screen:** Quick look at `SKILL.md` (scroll the description + the 4 workflows), then a one-line mental model on screen: `plain English -> snapexe-oss skill -> OpenSearch public APIs + boto3`. Lower-third: *"No proprietary services. No lock-in."*

**Narration:**
> "snapexe-oss is an agent skill. You ask, in plain English, to back up, migrate, or tear down a cluster, and the agent drives the whole lifecycle: snapshot, status, restore, and cleanup. The engine underneath is just Python over OpenSearch's public snapshot APIs and the standard AWS credential chain - no proprietary services, no lock-in. Let me show you a full backup-and-migrate, live, against two real clusters."

---

## Scene 3 - The source (1:00-1:25)

**On-screen:** Run these against the source and let them land (from `snapexe-demo.html` step 1). Highlight `logs` / `orders` / `users`, the `logs-datastream` data stream, and `logs-searchable` (`remote_snapshot`).

```
curl -sku "$SCRED" "https://localhost:9200/_cat/indices?v&s=index"
curl -sku "$SCRED" "https://localhost:9200/_data_stream?pretty"
```

**Narration:**
> "Here's our source cluster - two nodes, hot and warm. It has regular indices - logs, orders, users - a data stream, logs-datastream, and the hard one: logs-searchable, a searchable snapshot whose data already lives in S3. Three different beasts, each restored a different way. That's exactly what trips people up."

---

## Scene 4 - Destination is empty (1:25-1:40)

**On-screen:** `curl -sku "$DCRED" "https://localhost:9201/_cat/indices?v&s=index"` - only system indices. Lower-third: *"Destination: empty."*

**Narration:**
> "And here's the destination - a fresh, empty cluster. Only system indices. Nothing of ours. By the end of this video, everything from the source will be here, and provably queryable."

---

## Scene 5 - Back up the source (1:40-2:35)

**On-screen:** In Claude Code, paste the real backup prompt (demo step 3). Fast-forward the provisioning; land on the clean **"Snapshot started"** summary (S3 bucket / IAM user / repository).

```
Using the snapexe-oss skill: back up my source OpenSearch cluster at
https://localhost:9200 to S3 with tag "demo", using the Secrets Manager
secret snapexe/opensearch for credentials. It is a 2-node cluster (Docker
containers os-source-hot and os-source-warm) with no repository set up yet -
provision the bucket + IAM user and install the keystore keys on both nodes,
in us-west-2.
```

**Narration:**
> "Now the backup. One prompt: back up the source to S3, tag it 'demo', pull credentials from Secrets Manager, and set it up from scratch." *(let it run / cut forward)* "Watch what the skill just did on its own. It provisioned an S3 bucket and an IAM user, installed the repository-s3 keystore keys on both nodes - not just one, because the repository is verified cluster-wide - reloaded secure settings, and started the snapshot. That's roughly a dozen manual steps, and it never printed a secret. And notice it says 'started', not 'done' - because snapshots are asynchronous, and honest tools don't claim completion they can't see."

---

## Scene 6 - Status (2:35-2:50)

**On-screen:** Prompt `Show me the status of the "demo" snapshot.` Show the per-index heatmap, fast-forward to **SUCCESS 100%**.

**Narration:**
> "I ask for status. The skill queries the live snapshot API and renders a per-index heatmap. Fast-forwarding to completion - every shard, SUCCESS, one hundred percent. The backup is safely in S3."

---

## Scene 7 - Restore / migrate (2:50-3:45)

**On-screen:** Paste the real restore prompt (demo step 5). Fast-forward; land on **"Restore started"**. Optionally flash the restore-progress heatmap.

```
Using the snapexe-oss skill: restore the "demo" snapshot into the destination
cluster at https://localhost:9201, including the data stream and the searchable
snapshot. It is a 2-node cluster (os-dest-hot, os-dest-warm); use the secret
snapexe/opensearch.
```

**Narration:**
> "Now the real test - migrating into that empty cluster. One prompt: restore the 'demo' snapshot into the destination, including the data stream and the searchable snapshot. Here's where the hard parts happen automatically. Regular indices restore over the API. The data stream restores through the destination node's superadmin certificate - and because the snapshot carried global state, its index template travels with it and is recreated for us, with nothing to pre-build. And the searchable snapshot isn't re-copied - the skill re-points the destination at the same S3 backing snapshot. Three restore paths, one sentence."

---

## Scene 8 - Prove it's queryable (3:45-4:15)

**On-screen:** The proof (demo step 7) - three searches, each showing `hits.total.value > 0`.

```
curl -sku "$DCRED" "https://localhost:9201/logs/_search?pretty&size=1&track_total_hits=true"
curl -sku "$DCRED" "https://localhost:9201/logs-datastream/_search?pretty&size=1&track_total_hits=true"
curl -sku "$DCRED" "https://localhost:9201/logs-searchable/_search?pretty&size=1&track_total_hits=true"
```

**Narration:**
> "Presence isn't proof, so let's actually query the destination. The regular index - hits. The data stream, across its backing indices - hits. And the searchable snapshot, served straight from S3 on the destination - hits. The full cluster, three index types, migrated and queryable end to end, on a cluster that was empty ninety seconds ago."

---

## Scene 9 - Teardown (4:15-4:40)

**On-screen:** Paste the teardown prompt (demo step 8): dry-run preview -> "yes" -> **"Deleted"** summary. Flash one verify (`aws iam get-user --user-name snapexe-demo-user` -> `NoSuchEntity`). Lower-third: *"Dry-run + confirm before anything destructive."*

**Narration:**
> "Finally, cleanup - the step everyone forgets, and the one that quietly runs up your AWS bill. I ask it to tear down everything tagged 'demo'. It runs a dry-run first, shows exactly what will be deleted, and waits for my confirmation before touching anything. Confirmed - bucket, IAM user, keys, repository, config file: all gone. No orphaned resources."

---

## Scene 10 - Why it wins (4:40-5:00)

**On-screen:** Closing card - bullets + `github.com/madhankb/snapexe-oss` + "Hackathon submission #118".

**Narration:**
> "That's snapexe-oss. A day-long, expert-only runbook - backup, migration, and cleanup across data streams and searchable snapshots - reduced to plain-language requests, with safety built in and zero vendor lock-in. Production-ready code, tests, and docs are on GitHub today. Thanks for watching."

---

## Pre-flight checklist (before you hit record)

- **Dry-run the whole flow once end to end** so plugins/keys are warm and you know the timings. Then reset for the take.
- **Reset the destination to empty** (remove `logs`/`orders`/`users`/`logs-searchable`/`logs-datastream` and the dest-side repo registration) - Scene 4 must show only system indices.
- **Free an IAM key slot:** a full snapshot+restore cycle uses both of the user's 2 allowed keys. Make sure the `demo` tag's prior resources are torn down (`delete-all`) so provisioning doesn't hit `LimitExceeded` mid-take.
- **Confirm identity:** `aws sts get-caller-identity` shows account `765423874566`, region `us-west-2`; secret `snapexe/opensearch` exists.
- **Set the two shell vars (`$SCRED`, `$DCRED`)** off-camera, in the same shell you'll record - never show the Secrets Manager pull on screen.

## Recording & pacing notes

- **Handle the async waits in the edit, not on camera.** Snapshot and restore can take minutes. Record the prompt + the "started" summary live, cut, then cut back to the completed status/queries. Say "fast-forwarding to completion" so it's honest, never faked.
- **Terminal:** 18-20 pt font, ~1080p window, high-contrast theme, clear the scrollback before each scene. Turn on Do Not Disturb.
- **Two windows side by side** (Claude Code left, plain terminal for the curl proofs right) reads better than tab-switching.
- **Captions for muted viewers:** the lower-thirds double as on-screen text - many judges watch without sound first.
- **The one moment to nail:** Scene 8's three "hits > 0" lines are the proof. Let each land for a full beat before moving on.
