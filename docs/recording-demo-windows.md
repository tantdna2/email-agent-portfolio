# Quay video demo Email Agent trên Windows

**88 giây · lời thoại tiếng Anh · một cửa sổ PowerShell.** Video đi từ email mẫu của Maya đến bản nháp sửa ngắn hơn, rồi kiểm chứng rằng chỉ bản sửa được ghi vào hộp thư gửi mô phỏng. Đoạn cuối chiếu số liệu đánh giá đã lưu và lệnh chạy thử từ thư mục gốc của repo.

## Chuẩn bị FastAPI trong WSL Ubuntu

Mở hai cửa sổ Windows PowerShell tại thư mục gốc của bản checkout. Cửa sổ A chạy máy chủ. Cửa sổ B hiện demo và là cửa sổ duy nhất xuất hiện trong video.

Nếu máy chưa có Ubuntu, chạy `wsl --install -d Ubuntu` trong PowerShell với quyền quản trị, khởi động lại theo yêu cầu rồi mở Ubuntu để hoàn tất thiết lập. Kiểm tra thấy distro `Ubuntu` trong `wsl -l -v`. Các lệnh dưới đây đã được kiểm tra với Ubuntu 26.04.1 WSL 2 và Python 3.14.4. [Hướng dẫn cài WSL của Microsoft](https://learn.microsoft.com/en-us/windows/wsl/install)

Trong cửa sổ A, từ thư mục gốc repo, mở WSL. Bash sẽ vào thư mục tương ứng của bản checkout.

```powershell
wsl -d Ubuntu
```

Chạy trong Bash. Nếu thiếu mô-đun `venv`, cài bằng `sudo apt update && sudo apt install python3-venv` trước. Khâu cài đặt có thể cần Internet. Sau đó demo chỉ dùng localhost, fixture sẵn có, hai câu trả lời định sẵn và hộp thư gửi trong bộ nhớ.

```bash
python3 -m venv ~/.venvs/email-agent-demo
~/.venvs/email-agent-demo/bin/python -m pip install -r requirements-demo-api.txt
~/.venvs/email-agent-demo/bin/python -B -m uvicorn --app-dir scripts demo_draft_api:app --host 127.0.0.1 --port 8000 --workers 1
```

Chờ dòng `Application startup complete.` và giữ A chạy ngoài khung quay. Nếu cổng 8000 đang bận, dừng phiên demo cũ đang dùng cổng đó. Giữ địa chỉ `127.0.0.1:8000` để PowerShell gọi đúng máy chủ cục bộ.

Nếu máy đã có Docker chạy Linux containers, có thể thay bước khởi động Uvicorn bằng hai lệnh sau trong PowerShell. Cửa sổ B vẫn dùng WSL Ubuntu để đọc fixture và tính điểm từ tệp đã lưu.

```powershell
docker build -t email-agent-draft-demo .
docker run --rm -p 127.0.0.1:8000:8000 email-agent-draft-demo
```

## Chạy thử và chuẩn bị take mới

1. Ở cửa sổ B, chạy thử một lượt tự động từ thư mục gốc repo để xem đủ các màn hình. `-Rehearse` bỏ các điểm dừng và giữ toàn bộ kết quả để đọc lại.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\record-draft-demo.ps1 -Rehearse
   $LASTEXITCODE
   ```

   Kết quả mong đợi là mã thoát `0`, ba lượt duyệt `stale / sent / sent` với số lần gửi `0 / 1 / 1`, và dòng `PASS` sau khi kiểm tra đúng một thư có nội dung khớp chính xác với bản v2. Màn hình eval hiện 60 phản hồi trên 30 email, `27/30 → 28/30` về giữ đủ dữ kiện và `2/30` cần sửa cho mỗi prompt.

2. **Khởi động lại máy chủ trước mỗi take**, kể cả sau khi diễn tập hay hủy giữa chừng. Dừng helper ở B bằng `Ctrl+C` nếu còn chạy, hoặc nhấn Enter nếu đã dừng quay ở thẻ cuối. Ở A, nhấn `Ctrl+C`, chờ Uvicorn thoát rồi chạy lại đúng lệnh Uvicorn trên. Với Docker, dừng rồi chạy lại lệnh `docker run`. Phiên mới có trạng thái nháp và hộp thư gửi trống.

3. Ở B, đặt nền tối và phóng cỡ chữ khoảng 24–28 pt. Chọn chiều rộng đủ cho khoảng 76 ký tự và chiều cao đủ cho 12 dòng. Chạy helper theo từng bước **trước khi bật recorder**, đợi thấy tiêu đề `Email Agent | Approval after an edit` rồi giữ nguyên ở đó. Từ màn hình này, mỗi lần nhấn Enter sẽ chạy và hiển thị đúng bước kế tiếp.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\record-draft-demo.ps1
   ```

4. Để bản lời thoại trên màn hình thứ hai hoặc trên giấy. Bật **Do not disturb** của Windows. Giữ thanh taskbar, cửa sổ A, prompt có đường dẫn, thông báo, email thật, thiết lập tài khoản và token ngoài khung quay. Thẻ cuối chỉ hiện lệnh chạy demo từ thư mục gốc. Giữ trình duyệt đóng trong suốt take.

## Quay bằng công cụ của Windows

**Windows 11 · Snipping Tool.** Nhấn `Win+Shift+R`, kéo vùng quay quanh phần nội dung của cửa sổ B. Bật mic và chọn đúng thiết bị nhập, tắt âm thanh hệ thống. Bấm **Start** rồi trả focus về B bằng cách bấm vào khoảng trống trong vùng terminal. Bắt đầu lời thoại khi đếm ngược kết thúc. Đến thẻ cuối, bấm **Stop** trước khi nhấn Enter lần nữa. Mở video xem lại và dùng **Save as** để lưu bản đạt thành MP4, ví dụ `email-agent-demo.mp4`. [Các bước quay bằng Snipping Tool](https://support.microsoft.com/en-us/windows/apps/use-snipping-tool-to-capture-screenshots) · [Mic và âm thanh trong Snipping Tool](https://blogs.windows.com/windowsexperience/2023/09/26/the-most-personal-windows-11-experience-begins-rolling-out-today/)

**Phương án dự phòng · Xbox Game Bar.** Nếu Snipping Tool thiếu nút Record hoặc mic, thử với cửa sổ Windows Terminal chạy B. Mở `Win+G` để kiểm tra widget **Capture** và mic rồi đóng overlay. Khi B đang có focus, dùng `Win+Alt+R` để bắt đầu hoặc dừng, `Win+Alt+M` để đổi trạng thái mic. Game Bar ghi cửa sổ ứng dụng đang hoạt động. Xem lại tệp MP4 trong **Videos → Captures**. [Phím tắt và thư mục lưu của Game Bar](https://support.microsoft.com/en-us/accessibility/windows/use-a-screen-reader-to-record-your-screen-with-xbox-game-bar)

Quay thử 5 giây trên thẻ đầu để kiểm tra mic, cỡ chữ và khung hình. Giữ nguyên thẻ đầu nếu chưa nhấn Enter. Sau take chính, xem từ đầu đến cuối để kiểm tra tiếng, chữ và thẻ cuối. Nếu thấy `STOP`, dừng quay và khởi động lại phiên theo bước 2.

## Timeline và lời thoại

Đọc cột **English** với nhịp khoảng 115–130 từ/phút. Bản tiếng Việt để đối chiếu hoặc dùng làm phụ đề. Các mốc tính từ lúc video bắt đầu ghi hình.

| Mốc | Thao tác và nội dung trên màn hình | English | Tiếng Việt |
| --- | --- | --- | --- |
| 0:00–0:09 | Giữ thẻ mở đầu. Để thấy dòng `Canned drafts, real approval logic`, dòng mô phỏng Gmail và Telegram, và email mẫu `Workshop slides`. | This demo uses canned drafts and the app's real approval logic. Gmail and Telegram are simulated. | Demo dùng bản nháp định sẵn và logic duyệt thực tế của ứng dụng. Gmail và Telegram được mô phỏng. |
| 0:09–0:18 | Enter. `POST /draft`. Giữ chữ `Version: 1`, `awaiting_review` và toàn văn v1 trong khung. | Maya asks us to confirm receipt of the workshop slides. The API creates version one and waits for review. | Maya muốn được xác nhận đã nhận slide của buổi workshop. API tạo bản thứ nhất và chờ duyệt. |
| 0:18–0:28 | Enter. `POST /draft/edit`. Cho thấy `Make the reply shorter.`, `Version: 2` và câu trả lời ngắn hơn. | I ask for a shorter reply. The API creates version two with its own approval and waits for review again. | Tôi yêu cầu rút gọn câu trả lời. API tạo bản thứ hai với lượt duyệt riêng rồi lại chờ người dùng duyệt. |
| 0:28–0:38 | Enter. `Try the old v1 approval`. Giữ `stale` và `Simulated sends: 0`. | I try approving version one. The server marks that approval as stale, and the simulated send count stays at zero. | Tôi thử duyệt bản thứ nhất. Máy chủ báo lượt duyệt này đã hết hiệu lực. Số lần gửi mô phỏng vẫn là không. |
| 0:38–0:47 | Enter. `Approve the revised v2 draft`. Giữ `sent` và `Simulated sends: 1`. | Now I approve version two. The server returns sent, and the simulated send count goes up to one. | Giờ tôi duyệt bản thứ hai. Máy chủ trả về trạng thái đã gửi và số lần gửi mô phỏng tăng lên một. |
| 0:47–0:55 | Enter. `Replay the same v2 approval`. Giữ `sent` và số `1` để thấy kết quả lần bấm lại. | I replay the same approval. The send count stays at one. | Tôi bấm lại đúng lượt duyệt đó. Số lần gửi vẫn là một. |
| 0:55–1:04 | Enter. `GET /outbox`. Giữ dãy `0 / 1 / 1`, `Outbox messages: 1`, nội dung `Hi Maya, thanks for the slides. I received them.` và dòng `PASS`. | The simulated outbox contains one message. Its text matches version two exactly. | Hộp thư gửi mô phỏng có đúng một thư. Nội dung khớp hoàn toàn với bản thứ hai. |
| 1:04–1:19 | Enter. `Archived held-out prompt evaluation`. Giữ dòng 60 phản hồi trên 30 email, dòng `15 Vietnamese / 15 English` và cả hai cột Baseline và Candidate. | The archive covers sixty model responses across thirty held-out emails. The candidate kept every marked fact in twenty-eight cases versus twenty-seven. Each prompt had two drafts needing edits. | Bộ kết quả lưu 60 phản hồi của mô hình trên 30 email dành riêng để đánh giá. Prompt thử nghiệm giữ đủ dữ kiện được đánh dấu ở 28 trường hợp, so với 27 của prompt gốc. Mỗi prompt có hai bản nháp cần chỉnh. |
| 1:19–1:28 | Enter. Giữ nguyên thẻ lệnh chạy thử. Dừng recorder ở 1:28 rồi mới nhấn Enter để thoát helper. | Follow the README or run this offline demo command from the repo root. | Làm theo README hoặc chạy lệnh demo ngoại tuyến này từ thư mục gốc. |

## Đối chiếu sau khi quay

Helper lấy email mẫu và câu sửa từ [fixture dùng chung](../scripts/demo_daily_sender_digest.py). Nó giữ các token trong bộ nhớ, chỉ chiếu những trường đã chọn và kiểm tra phản hồi của API qua từng lượt. Bảng đánh giá dùng các điểm tính lại từ [bài đánh giá held-out đã lưu](../evals/results/heldout-a3160ef/README.md). Đây là 30 email tổng hợp, mỗi prompt sinh một bản nháp trên mỗi email, chấm mù bởi một reviewer AI. Chênh lệch một trường hợp về dữ kiện chỉ mô tả lần đánh giá này.

Từ gốc repo trong PowerShell, có thể tính lại bảng đầy đủ và chạy lại kịch bản offline bằng hai lệnh sau. Cả hai chạy độc lập với phiên Uvicorn.

```powershell
wsl -d Ubuntu -- python3 -B scripts/evaluate_daily_drafts.py report --run-dir evals/results/heldout-a3160ef
wsl -d Ubuntu -- python3 scripts/demo_daily_sender_digest.py
```
