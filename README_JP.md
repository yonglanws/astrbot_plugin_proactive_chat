# プライベート主動メッセージ · インタラクション強度 + 擬人化版

> [Pancakes-Labs/astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) の私聊専用フォークです。
>
> **グループへの主動メッセージは完全に削除しました。** グループ設定・グループタイマー・グループ UI はありません。
> 「インタラクション強度」に加えて、v2.3.0 では作息リズム・関係状態・主動意図・返信品質・自然度ガード・気泡送信・気分/精力・LivingMemory 連携を追加しました。
> v2.4.0 では AstrBot ネイティブのプラグインページ（`views/proactive-chat`）を追加し、Dashboard のテーマに追従します。独立 WebUI も引き続き利用できます。

完全なドキュメントは中国語版 [README.md](README.md) と [docs/](docs/) を参照してください。

主なポイント：

- 設定ルートは `private_settings`（旧 `friend_settings` は自動移行）
- 私聊 UMO の形式：`qq_official:FriendMessage:<openid>`
- 強度スコア 0–100 → 確率 15%–95%、間隔は基礎値の 35%–115%
- 活動時間帯の学習、1 日の上限、最小間隔
- 関係状態と主動意図（話題継続 / 新話題 / 気遣い / 自己開示 / 質問 / 今回は話さない）
- 返信品質の分類とネガティブフィードバック時の一時停止（敷衍・負のフィードバックは返信率に計上しない）
- 送信前の自然度ガード（重複・長さ・連続質問・AI 口調）
- 1–3 個の気泡送信と確率的な音声送信
- 短期工作記憶：未完了の話題、ユーザーの計画/イベント、未回答の質問、軽量プロフィール
- 任意の planner / writer / critic 多段階生成
- オフライン再生ツール：`tools/replay_humanization.py`
- WebUI に「互动强度（インタラクション強度）」ページと擬人化バッジを追加
- 履歴の強度シード：`tools/import_astrbot_history.py`
- テスト：`python -m pytest tests -q`（180 件）

ライセンスは元プロジェクトに従います（[LICENSE](LICENSE)）。
