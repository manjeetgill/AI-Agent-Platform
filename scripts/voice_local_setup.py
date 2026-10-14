"""Configure the isolated voice pilot using installed local models; no downloads or provider keys."""
import argparse
import os
from pathlib import Path

os.environ.setdefault('AGENT_LOCAL_PROFILE', 'voice')
if os.environ['AGENT_LOCAL_PROFILE'] != 'voice':
    raise SystemExit('This command configures only AGENT_LOCAL_PROFILE=voice')

from local import CONFIG, load, save, setup

from agent_platform import local_speech, voice_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asr-model', type=Path, required=True, help='Installed multilingual whisper.cpp model')
    parser.add_argument('--model', required=True, help='Exact installed model ID, for example qwen2.5:3b')
    parser.add_argument('--model-url', default='http://127.0.0.1:11435/v1/chat/completions')
    parser.add_argument('--language', choices=['auto', 'en', 'hi'], default='auto')
    args = parser.parse_args()
    profile = voice_config.VoiceConfig(asr='local', tts='local', reasoning='local', model=args.model)
    settings = {
        'AI_WEB_VOICE_ENABLED': '1', 'AI_CALLING_SPEECH_PROVIDER': 'local',
        'AI_WEB_VOICE_PROVIDER': 'local', 'AI_WEB_VOICE_MODEL': profile.model,
        'AI_VOICE_LOCAL_MODEL_URL': args.model_url,
        'AI_LOCAL_STT_MODEL': str(args.asr_model.expanduser().resolve()),
        'AI_LOCAL_STT_LANGUAGE': args.language,
        'AI_VOICE_MONTHLY_SESSION_LIMIT': '300',
    }
    os.environ.update(settings)
    # Validate before modifying configuration. The model service can be started afterwards.
    local_speech.assets()
    with voice_config.using(profile.model_dump()):
        voice_config.gateway()
    setup()
    values = load()
    values.update(settings)
    save(CONFIG, values)
    print('Isolated local voice configuration saved. No provider credentials were added.')
    print('Start the installed local model server, then run:')
    print('AGENT_LOCAL_PROFILE=voice python scripts/local.py start')
    print('Workspace: http://127.0.0.1:4161/voice. Run scripts/verify_voice_local.py to verify audio.')


if __name__ == '__main__':
    main()
