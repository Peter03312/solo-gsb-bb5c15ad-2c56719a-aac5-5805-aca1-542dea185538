#!/usr/bin/env bash
# 针对运行中的服务（默认 http://localhost:8000）演示完整业务流程。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "健康检查"
curl -s "$BASE/health"; echo

say "判定示例：demo-derived-1 在 2026-03-01 可用于 research（交集仅 research）"
curl -s "$BASE/permissions/check?version_id=demo-derived-1&use=research&at=2026-03-01T00:00:00Z"; echo

say "判定示例：2026-07-01 申请 review（被 demo-root-a 阻断）"
curl -s "$BASE/permissions/check?version_id=demo-derived-1&use=review&at=2026-07-01T00:00:00Z"; echo

say "区间分段：demo-derived-1 的 review 在 2026-01-15 ~ 2026-07-15 的可用片段与阻断原因"
curl -s "$BASE/permissions/timeline?version_id=demo-derived-1&use=review&start=2026-01-15T00:00:00Z&end=2026-07-15T00:00:00Z"; echo

say "区间分段：同一区间申请 research（2 月 1 日起持续允许，6 月 1 日依据变更仍切分片段）"
curl -s "$BASE/permissions/timeline?version_id=demo-derived-1&use=research&start=2026-01-15T00:00:00Z&end=2026-07-15T00:00:00Z"; echo

say "区间分段错误：start 不早于 end -> 422 invalid_interval"
curl -s "$BASE/permissions/timeline?version_id=demo-derived-1&use=research&start=2026-07-15T00:00:00Z&end=2026-01-15T00:00:00Z"; echo

say "登记新派生版本（父版本 demo-root-a）"
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"example-v1","parent_ids":["demo-root-a"],"note":"调用示例"}'; echo

say "提交许可变更"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}'; echo

say "重复提交：同标识同内容 -> 200 replayed=true"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research","research"]}'; echo

say "冲突 1：同标识不同内容 -> 409"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["commercial"]}'; echo

say "冲突 2：同版本同生效时刻不同变更 -> 409"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c2","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}'; echo

say "无有效许可（变更生效之前）-> allowed=false"
curl -s "$BASE/permissions/check?version_id=example-v1&use=research&at=2026-02-01T00:00:00Z"; echo

say "预演（不写入）：假设 demo-root-a 自 2026-04-01 起放开 commercial"
curl -s -X POST "$BASE/license-changes/preview" -H 'Content-Type: application/json' \
  -d '{"change_id":"preview-a-commercial","version_id":"demo-root-a","effective_at":"2026-04-01T00:00:00Z","allowed_uses":["research","review","commercial"],"use":"commercial","at":"2026-05-01T00:00:00Z"}'; echo

say "预演：查询时刻早于生效时刻 -> impacted_versions 为空（不误报）"
curl -s -X POST "$BASE/license-changes/preview" -H 'Content-Type: application/json' \
  -d '{"change_id":"preview-a-commercial","version_id":"demo-root-a","effective_at":"2026-04-01T00:00:00Z","allowed_uses":["research","review","commercial"],"use":"commercial","at":"2026-03-01T00:00:00Z"}'; echo

say "预演：相同标识与内容重放 -> replayed=true 且无影响（不写入）"
curl -s -X POST "$BASE/license-changes/preview" -H 'Content-Type: application/json' \
  -d '{"change_id":"demo-chg-a-1","version_id":"demo-root-a","effective_at":"2026-01-01T00:00:00Z","allowed_uses":["review","research"],"use":"research","at":"2026-05-01T00:00:00Z"}'; echo

say "作废：作废 example-c1（登记有误），返回回执"
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","revocation_id":"rev-example-c1","reason":"允许用途登记有误，撤回"}'; echo

say "作废重试：同一作废标识与内容 -> 200 replayed=true，返回原回执"
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","revocation_id":"rev-example-c1","reason":"允许用途登记有误，撤回"}'; echo

say "作废冲突 1：同一作废标识换原因 -> 409"
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","revocation_id":"rev-example-c1","reason":"换个原因"}'; echo

say "作废冲突 2：另一作废标识重复作废同一变更 -> 409"
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","revocation_id":"rev-example-c1-again","reason":"允许用途登记有误，撤回"}'; echo

say "作废目标不存在 -> 404"
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"no-such-change","revocation_id":"rev-ghost","reason":"x"}'; echo

say "作废追溯：按作废标识取回回执"
curl -s "$BASE/license-changes/revocations/rev-example-c1"; echo

say "原变更仍可按原接口查询，附带 revocation 回执"
curl -s "$BASE/license-changes/example-c1"; echo

say "作废立即对所有查询时刻生效：example-v1 再无未作废变更 -> no_valid_license"
curl -s "$BASE/permissions/check?version_id=example-v1&use=research&at=2026-05-01T00:00:00Z"; echo

say "重放已作废变更不能恢复：仍 replayed=true 且带 revocation"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}'; echo

say "判定凭证：首次签发 demo-derived-1 的 review @ 2026-03-01（被 root-b 拒绝，拒绝也留痕），201"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-1","version_id":"demo-derived-1","use":" review ","at":"2026-03-01T08:00:00+08:00"}'; echo

say "凭证重试：去空格用途 + 同一 UTC 瞬间（+08:00 与 Z 等价）-> 200 replayed=true，原凭证逐字段一致"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-1","version_id":"demo-derived-1","use":"review","at":"2026-03-01T00:00:00Z"}'; echo

say "凭证追溯：按标识取回首次保存的判定"
curl -s "$BASE/permissions/vouchers/example-voucher-1"; echo

say "凭证冲突：同一标识换用途 -> 409（原凭证不变）"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-1","version_id":"demo-derived-1","use":"research","at":"2026-03-01T00:00:00Z"}'; echo

say "凭证错误 1：未知版本 -> 404，不留凭证"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-x","version_id":"no-such-version","use":"research","at":"2026-03-01T00:00:00Z"}'; echo

say "凭证错误 2：无时区时刻 -> 422，不留凭证"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-x","version_id":"demo-derived-1","use":"research","at":"2026-03-01T00:00:00"}'; echo

say "凭证错误 3：空用途 -> 422，不留凭证"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-x","version_id":"demo-derived-1","use":"   ","at":"2026-03-01T00:00:00Z"}'; echo

say "凭证错误 4：未知凭证标识 -> 404"
curl -s "$BASE/permissions/vouchers/no-such-voucher"; echo

say "凭证不可变：允许的凭证签发后再收紧/作废，GET 仍返回首次结果"
curl -s -X POST "$BASE/permissions/vouchers" -H 'Content-Type: application/json' \
  -d '{"voucher_id":"example-voucher-2","version_id":"demo-derived-1","use":"research","at":"2026-05-01T00:00:00Z"}'; echo
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"demo-chg-d1-1","revocation_id":"rev-demo-d1-1","reason":"示例：收紧后凭证仍应保留首次结果"}' >/dev/null || true
echo "# 实时判定此刻可能已变化："
curl -s "$BASE/permissions/check?version_id=demo-derived-1&use=research&at=2026-05-01T00:00:00Z"; echo
echo "# 而凭证恒为首次保存的 allowed=true："
curl -s "$BASE/permissions/vouchers/example-voucher-2"; echo

say "许可结果订阅：自包含示例版本 sub-demo-v1 先登记并提交 2026-03 起仅 research"
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"sub-demo-v1","parent_ids":[],"note":"许可结果订阅调用示例"}' >/dev/null || true
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c1","version_id":"sub-demo-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}' >/dev/null || true

say "建立订阅：关注 sub-demo-v1 在固定时刻 2026-09-01 对 review 的许可（基线为拒绝），201"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type: application/json' \
  -d '{"subscription_id":"example-sub-1","version_id":"sub-demo-v1","use":" review ","at":"2026-09-01T08:00:00+08:00"}'; echo

say "订阅重试：去空格用途 + 同一 UTC 瞬间（+08:00 与 Z 等价）-> 200 replayed=true，原订阅逐字段一致"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type: application/json' \
  -d '{"subscription_id":"example-sub-1","version_id":"sub-demo-v1","use":"review","at":"2026-09-01T00:00:00Z"}'; echo

say "订阅追溯：按订阅标识取回不可变基线"
curl -s "$BASE/permissions/subscriptions/example-sub-1"; echo

say "触发翻转 1：2026-06 起放开 review（拒绝 -> 允许），变更与事件同一事务落库"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c2","version_id":"sub-demo-v1","effective_at":"2026-06-01T00:00:00Z","allowed_uses":["research","review"]}'; echo

say "触发翻转 2：再收紧回仅 research（允许 -> 拒绝）"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c3","version_id":"sub-demo-v1","effective_at":"2026-07-01T00:00:00Z","allowed_uses":["research"]}'; echo

say "触发翻转 3：作废 sub-demo-c3（拒绝 -> 允许；作废与事件同一事务）"
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c3","revocation_id":"sub-demo-rev-c3","reason":"收紧登记有误，撤回"}'; echo

say "变更/作废重试不产生重复事件（返回 200 replayed=true，事件数不变）"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c2","version_id":"sub-demo-v1","effective_at":"2026-06-01T00:00:00Z","allowed_uses":["review","research"]}'; echo
curl -s -X POST "$BASE/license-changes/revocations" -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c3","revocation_id":"sub-demo-rev-c3","reason":"收紧登记有误，撤回"}'; echo

say "读取翻转事件：按序号游标从头读（稳定升序、逐序号递增）"
curl -s "$BASE/permissions/subscriptions/example-sub-1/events?after_seq=0&limit=2"; echo

say "游标翻页：带上一页 next_cursor=2 继续读"
curl -s "$BASE/permissions/subscriptions/example-sub-1/events?after_seq=2&limit=10"; echo

say "订阅冲突：同一标识换用途 -> 409（原订阅不变）"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type: application/json' \
  -d '{"subscription_id":"example-sub-1","version_id":"sub-demo-v1","use":"commercial","at":"2026-09-01T00:00:00Z"}'; echo

say "订阅错误 1：未知版本 -> 404，不留订阅"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type: application/json' \
  -d '{"subscription_id":"example-sub-x","version_id":"no-such-version","use":"research","at":"2026-09-01T00:00:00Z"}'; echo

say "订阅错误 2：无时区时刻 -> 422，不留订阅"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type: application/json' \
  -d '{"subscription_id":"example-sub-x","version_id":"sub-demo-v1","use":"research","at":"2026-09-01T00:00:00"}'; echo

say "订阅错误 3：空用途 -> 422，不留订阅"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type:application/json' \
  -d '{"subscription_id":"example-sub-x","version_id":"sub-demo-v1","use":"   ","at":"2026-09-01T00:00:00Z"}'; echo

say "订阅错误 4：未知订阅标识（GET/事件流）-> 404"
curl -s "$BASE/permissions/subscriptions/no-such-subscription"; echo
curl -s "$BASE/permissions/subscriptions/no-such-subscription/events"; echo

say "批量许可变更：自包含示例版本 batch-demo-v1 先登记并提交 2026-03 起仅 research"
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"batch-demo-v1","parent_ids":[],"note":"批量许可变更调用示例"}' >/dev/null || true
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"batch-demo-c1","version_id":"batch-demo-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}' >/dev/null || true

say "建立订阅：关注 batch-demo-v1 在 2026-11 固定时刻对 review 的许可（基线拒绝）"
curl -s -X POST "$BASE/permissions/subscriptions" -H 'Content-Type: application/json' \
  -d '{"subscription_id":"example-batch-sub","version_id":"batch-demo-v1","use":"review","at":"2026-11-01T00:00:00Z"}' >/dev/null || true

say "批量导入（同一事务按序应用）：6 月放开 review（拒绝->允许）、8 月再收紧（允许->拒绝），两次真实翻转都留痕，201"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "example-batch-1",
    "changes": [
      {"change_id":"batch-demo-c2","version_id":"batch-demo-v1","effective_at":"2026-06-01T00:00:00Z","allowed_uses":["research","review"]},
      {"change_id":"batch-demo-c3","version_id":"batch-demo-v1","effective_at":"2026-08-01T00:00:00Z","allowed_uses":["research"]}
    ]
  }'; echo

say "读取事件：同批后续翻回也保留两次事件（seq 1 与 seq 2），逐条 new_event_count 均为 1"
curl -s "$BASE/permissions/subscriptions/example-batch-sub/events?after_seq=0&limit=10"; echo

say "批次幂等重试：同批次标识 + 规范化后完全相同的列表（用途换序/重复等价）-> 200 replayed=true，返回首次冻结结果且不重放事件"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "example-batch-1",
    "changes": [
      {"change_id":"batch-demo-c2","version_id":"batch-demo-v1","effective_at":"2026-06-01T00:00:00Z","allowed_uses":["review","research","review"]},
      {"change_id":"batch-demo-c3","version_id":"batch-demo-v1","effective_at":"2026-08-01T08:00:00+08:00","allowed_uses":["research"]}
    ]
  }'; echo

say "按批次标识取回首次冻结结果"
curl -s "$BASE/license-changes/batches/example-batch-1"; echo

say "批次冲突 1：同一批次标识换内容 -> 409（不应用、不重放事件）"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{"batch_id":"example-batch-1","changes":[{"change_id":"batch-demo-c9","version_id":"batch-demo-v1","effective_at":"2026-09-01T00:00:00Z","allowed_uses":["research"]}]}'; echo

say "批次冲突 2：批内变更标识重复 -> 409，整批拒绝（变更与事件均不落库）"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{"batch_id":"example-batch-dup","changes":[
    {"change_id":"dup-c","version_id":"batch-demo-v1","effective_at":"2026-04-01T00:00:00Z","allowed_uses":["research"]},
    {"change_id":"dup-c","version_id":"batch-demo-v1","effective_at":"2026-05-01T00:00:00Z","allowed_uses":["research"]}
  ]}'; echo

say "批次错误：未知版本 -> 404，整批拒绝（连同一批中的合法变更也不落库）"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{"batch_id":"example-batch-404","changes":[
    {"change_id":"ok-c","version_id":"batch-demo-v1","effective_at":"2026-04-01T00:00:00Z","allowed_uses":["research"]},
    {"change_id":"ghost-c","version_id":"no-such-version","effective_at":"2026-04-01T00:00:00Z","allowed_uses":["research"]}
  ]}'; echo

say "批次错误：空列表 / 无时区时刻 -> 422，整批拒绝"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' -d '{"batch_id":"example-batch-empty","changes":[]}'; echo
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{"batch_id":"example-batch-naive","changes":[{"change_id":"naive-c","version_id":"batch-demo-v1","effective_at":"2026-04-01T00:00:00","allowed_uses":["research"]}]}'; echo

say "批内重放已存变更：与已存标识内容完全相同的一条 replayed=true 且 new_event_count=0，新变更照常翻转"
curl -s -X POST "$BASE/license-changes/batches" -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "example-batch-mix",
    "changes": [
      {"change_id":"batch-demo-c1","version_id":"batch-demo-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]},
      {"change_id":"batch-demo-c4","version_id":"batch-demo-v1","effective_at":"2026-09-01T00:00:00Z","allowed_uses":["research","review"]}
    ]
  }'; echo

say "版本槽发布：登记演示用版本链 release-demo-root -> release-demo-v1 -> release-demo-v2"
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"release-demo-root","parent_ids":[],"note":"槽发布演示根版本"}'; echo
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"release-demo-v1","parent_ids":["release-demo-root"]}'; echo
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"release-demo-v2","parent_ids":["release-demo-v1"]}'; echo

say "首次统一发布：两个槽（prod 首版、canary 首版），预期修订号均为 0 -> 201，各槽修订=0"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{
    "release_id": "example-release-1",
    "effective_at": "2026-08-01T00:00:00Z",
    "slots": [
      {"slot": " prod ", "version_id": "release-demo-v1", "expected_revision": 0},
      {"slot": "canary", "version_id": "release-demo-root", "expected_revision": 0}
    ]
  }'; echo

say "发布重试：槽名空格差异与 +08:00 同一 UTC 瞬间均为规范化后相同内容 -> 200 replayed=true，修订不再推进"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{
    "release_id": "example-release-1",
    "effective_at": "2026-08-01T08:00:00+08:00",
    "slots": [
      {"slot": "prod", "version_id": "release-demo-v1", "expected_revision": 0},
      {"slot": " canary ", "version_id": "release-demo-root", "expected_revision": 0}
    ]
  }'; echo

say "发布冲突 1：同一发布标识换内容 -> 409（首次冻结结果不变）"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-1","effective_at":"2026-08-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"release-demo-v1","expected_revision":0}]}'; echo

say "发布冲突 2：同一次发布内槽名重复（去空格后）-> 422"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-dup","effective_at":"2026-09-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"release-demo-v1","expected_revision":0},
                {"slot":" prod ","version_id":"release-demo-v1","expected_revision":0}]}'; echo

say "发布冲突 3：prod 第二次发布目标不是严格后代（指向自己）-> 409，整次不生效"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-bad-self","effective_at":"2026-09-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"release-demo-v1","expected_revision":1}]}'; echo

say "发布冲突 4：预期修订号不匹配（当前修订 0，下一次应为 1，提交 5）-> 409"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-bad-rev","effective_at":"2026-09-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"release-demo-v2","expected_revision":5}]}'; echo

say "发布冲突 5：生效时刻不晚于该槽上次发布（2026-08-01）-> 409"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-bad-time","effective_at":"2026-07-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"release-demo-v2","expected_revision":1}]}'; echo

say "发布冲突 6：目标版本不存在 -> 404，整次拒绝且不落任何记录"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-404","effective_at":"2026-09-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"no-such-version","expected_revision":1}]}'; echo

say "第二次合法发布：prod 推进到严格后代 release-demo-v2，修订 0 -> 1 -> 201"
curl -s -X POST "$BASE/releases" -H 'Content-Type: application/json' \
  -d '{"release_id":"example-release-2","effective_at":"2026-09-01T00:00:00Z",
       "slots":[{"slot":"prod","version_id":"release-demo-v2","expected_revision":1}]}'; echo

say "查询：全部发布 / 按发布标识取回首次冻结记录 / 全部槽当前指针 / 单槽当前指针"
curl -s "$BASE/releases"; echo
curl -s "$BASE/releases/example-release-1"; echo
curl -s "$BASE/slots"; echo
curl -s "$BASE/slots/by-name/prod"; echo

say "历史解析 1：2026-08-15（发布 1 之后、发布 2 之前）prod 当时指向 v1，修订 0"
curl -s "$BASE/slot-resolutions/prod?at=2026-08-15T00:00:00Z"; echo

say "历史解析 2：2026-09-15 prod 已推进到 v2，修订 1（+08:00 同一 UTC 瞬间等价）"
curl -s "$BASE/slot-resolutions/prod?at=2026-09-15T08:00:00%2B08:00"; echo

say "历史解析 3：恰在生效时刻即采用新发布（2026-09-01T00:00:00Z -> v2）"
curl -s "$BASE/slot-resolutions/prod?at=2026-09-01T00:00:00Z"; echo

say "历史解析错误 1：早于该槽首次发布 -> 404 slot_not_published / no_release_effective_at_or_before"
curl -s "$BASE/slot-resolutions/prod?at=2026-01-01T00:00:00Z"; echo

say "历史解析错误 2：槽从未发布 -> 404 slot_not_published / slot_never_published"
curl -s "$BASE/slot-resolutions/no-such-slot?at=2026-09-01T00:00:00Z"; echo

say "历史复现判定：解析槽当时版本后按现有规则判定用途（2026-08-15 与 2026-09-15 可能不同）"
curl -s "$BASE/slot-permissions/prod?use=research&at=2026-08-15T00:00:00Z"; echo
curl -s "$BASE/slot-permissions/prod?use=research&at=2026-09-15T00:00:00Z"; echo

say "交付准入：版本条目全窗口持续允许 research -> 201，原子固化不可变记录（含各段解析版本与判定依据）"
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{
    "delivery_id": "example-delivery-1",
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-12-01T00:00:00Z",
    "entries": [
      {"kind": "version", "ref": "release-demo-v1", "use": "research"},
      {"kind": "slot", "ref": " prod ", "use": " research "}
    ]
  }'; echo

say "交付准入重试：引用/用途去空格、+08:00 同一 UTC 瞬间、顺序一致 -> 200 replayed=true，返回首次记录"
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{
    "delivery_id": " example-delivery-1 ",
    "start": "2026-08-01T08:00:00+08:00",
    "end": "2026-12-01T08:00:00+08:00",
    "entries": [
      {"kind": "version", "ref": "release-demo-v1", "use": "research"},
      {"kind": "slot", "ref": "prod", "use": "research"}
    ]
  }'; echo

say "交付准入追溯：按交付号取回首次固化记录（之后目录变化不改写它）"
curl -s "$BASE/delivery-admissions/example-delivery-1"; echo

say "交付准入冲突：同一交付号换窗口/换条目（含换序）-> 409，首次记录不变"
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-1","start":"2026-08-01T00:00:00Z","end":"2027-01-01T00:00:00Z",
       "entries":[{"kind":"version","ref":"release-demo-v1","use":"research"}]}'; echo

say "交付准入拒绝：窗口早于 prod 首次发布 -> 403，首个失败时段 [07-01,08-01) 原因 slot_not_published，且不留记录"
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-early","start":"2026-07-01T00:00:00Z","end":"2026-08-15T00:00:00Z",
       "entries":[{"kind":"slot","ref":"prod","use":"research"}]}'; echo
echo "# 被拒绝的交付号 GET 为 404："
curl -s "$BASE/delivery-admissions/example-delivery-early"; echo

say "交付准入拒绝：槽从未发布（整段失败）/ 用途中途被收回（首个失败时段与 use_not_allowed 依据）"
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-neverslot","start":"2026-08-01T00:00:00Z","end":"2026-12-01T00:00:00Z",
       "entries":[{"kind":"slot","ref":"no-such-slot","use":"research"}]}'; echo
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-review","start":"2026-07-01T00:00:00Z","end":"2026-10-01T00:00:00Z",
       "entries":[{"kind":"version","ref":"demo-root-a","use":"review"}]}'; echo

say "交付准入错误：未知版本 404 / 无时区 / 空窗口 / 空白用途 / 空清单 均为 4xx 且整单不落库"
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-404","start":"2026-08-01T00:00:00Z","end":"2026-12-01T00:00:00Z",
       "entries":[{"kind":"version","ref":"no-such-version","use":"research"}]}'; echo
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-bad1","start":"2026-08-01T00:00:00","end":"2026-12-01T00:00:00Z",
       "entries":[{"kind":"version","ref":"demo-root-a","use":"research"}]}'; echo
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-bad2","start":"2026-12-01T00:00:00Z","end":"2026-12-01T00:00:00Z",
       "entries":[{"kind":"version","ref":"demo-root-a","use":"research"}]}'; echo
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-bad3","start":"2026-08-01T00:00:00Z","end":"2026-12-01T00:00:00Z",
       "entries":[{"kind":"version","ref":"demo-root-a","use":"   "}]}'; echo
curl -s -X POST "$BASE/delivery-admissions" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"example-delivery-bad4","start":"2026-08-01T00:00:00Z","end":"2026-12-01T00:00:00Z","entries":[]}'; echo

say "证明包：首次导出 example-voucher-1 -> 201（签名并原子固化）"
curl -s -X POST "$BASE/permissions/vouchers/example-voucher-1/proof"; echo

say "证明包重复导出 -> 200 replayed=true，返回同一内容（旧包不重签）"
curl -s -X POST "$BASE/permissions/vouchers/example-voucher-1/proof"; echo

say "证明包只读取回（GET 不触发签名；未导出过的凭证返回 404 proof_not_exported）"
curl -s "$BASE/permissions/vouchers/example-voucher-1/proof"; echo

say "证明包：未知凭证 -> 404 not_found（不留任何证明行）"
curl -s -X POST "$BASE/permissions/vouchers/no-such-voucher/proof"; echo

say "公钥：列出全部保留公钥（含轮换前旧公钥）与当前活跃密钥"
curl -s "$BASE/signing/keys"; echo

say "轮换活跃签名密钥：生成并启用新密钥，旧公钥仍可取回"
curl -s -X POST "$BASE/signing/keys/rotate"; echo

say "轮换后旧证明包仍是原签名（kid 不变、不重签），用旧公钥仍可离线验签"
curl -s -X POST "$BASE/permissions/vouchers/example-voucher-1/proof"; echo

echo
echo "离线验签（把证明包存为文件后）："
echo "  curl -s -X POST $BASE/permissions/vouchers/example-voucher-1/proof > proof.json"
echo "  python3 examples/offline-verify-proof.py proof.json"
echo "  # 用独立渠道取得的公钥交叉核验："
echo "  KID=\$(python3 -c \"import json;print(json.load(open('proof.json'))['proof']['signature']['key_id'])\")"
echo "  curl -s $BASE/signing/keys/\$KID/public | python3 -c \"import json,sys;print(json.load(sys.stdin)['public_key_pem'])\" > key.pem"
echo "  python3 examples/offline-verify-proof.py proof.json --public-key key.pem"
