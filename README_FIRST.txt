CHZZK · SOOP Recorder 배포판

처음 사용하는 순서

1. FFmpeg를 별도 설치하고 PATH에 추가하거나 tools\ffmpeg.exe로 놓습니다.
2. install_dependencies.bat를 실행합니다.
3. database_schema.sql을 MySQL/MariaDB에서 실행합니다.
4. config.json의 database 사용자 이름과 비밀번호를 본인 정보로 바꿉니다.
5. 필요하면 config.json의 저장 경로를 바꿉니다.
6. start_all_in_one.bat를 실행합니다.
7. 브라우저에서 http://127.0.0.1:8765 를 엽니다.
8. 스트리머 관리에서 CHZZK 또는 SOOP 채널을 등록합니다.

자세한 설명은 RECORDER_사용법.txt를 읽으십시오.

기본 보안 상태

- 스트리머 목록 비어 있음
- YouTube 업로드 OFF
- AI 썸네일 OFF
- 업로드 후 자동 파일 삭제 OFF
- DB 비밀번호는 CHANGE_ME 예시값
- 개인 쿠키, OAuth 토큰, 영상, 로그 미포함
- FFmpeg 실행 파일 미포함(사용자가 별도 설치)

주의

- config.json의 CHANGE_ME를 실제 비밀번호로 바꾸기 전에는 DB 연결이 되지 않습니다.
- 개인 설정을 마친 config.json은 다른 사람과 공유하지 마십시오.
- 녹화물의 이용에는 저작권과 각 플랫폼 이용약관이 적용됩니다.
