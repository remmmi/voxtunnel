@echo off
rem voxtunnel.cmd (Windows) - streams the local microphone to the
rem snd-aloop loopback of a remote Linux server, over SSH.
rem
rem EXPERIMENTAL: CI-tested only, never validated on real Windows hardware.
rem
rem   voxtunnel.cmd --list                     list capture devices
rem   voxtunnel.cmd user@vps                   stream with the first mic
rem   voxtunnel.cmd user@vps "Exact Mic Name"  stream with a given mic
rem
rem Requires: winget install ffmpeg  (and the built-in OpenSSH client,
rem with a key in %USERPROFILE%\.ssh - password auth will not work).

if "%~1"=="--list" (
  ffmpeg -hide_banner -list_devices true -f dshow -i dummy
  exit /b 0
)
if "%~1"=="" (
  echo usage: voxtunnel.cmd user@vps ["Exact Mic Name"]  ^|  voxtunnel.cmd --list
  exit /b 2
)

set VPS=%~1
set MIC=%~2
if "%MIC%"=="" (
  echo No mic name given: run "voxtunnel.cmd --list" and pass the exact
  echo audio device name as second argument.
  exit /b 2
)

echo voxtunnel - "%MIC%" to %VPS% (Ctrl-C to stop)
ffmpeg -hide_banner -loglevel error -f dshow -audio_buffer_size 20 ^
       -i audio="%MIC%" -f s16le -ar 48000 -ac 1 - | ^
ssh -o BatchMode=yes -o ConnectTimeout=10 -o Compression=no %VPS% ^
    "aplay -D plughw:Loopback,1,0 -f S16_LE -c 1 -r 48000 -t raw -q --buffer-time=80000 --period-time=20000"
