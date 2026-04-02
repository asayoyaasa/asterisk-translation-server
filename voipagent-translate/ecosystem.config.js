module.exports = {
  apps: [{
    name: 'voipagent-translate',
    script: 'src/server.js',
    cwd: '/opt/voipagent-translate',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '256M',
    env: {
      NODE_ENV: 'production',
      PORT: 4010,
      HOST: '127.0.0.1',
      TRANSLATION_EVENT_LOG: '/opt/voipagent-translate/data/translation-events.jsonl',
      RECENT_CALL_HOURS: 12,
      MAX_EVENTS_PER_CALL: 500
    },
    error_file: '/var/log/voipagent-translate-error.log',
    out_file: '/var/log/voipagent-translate-out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }]
};
