# /etc/profile.d/no_proxy.sh
# Устанавливает NO_PROXY для всех процессов на сервере.
# Деплоится автоматически скриптом deploy.sh в /etc/profile.d/
export NO_PROXY="localhost,127.0.0.1,10.1.5.97,10.1.5.6,10.1.26.2,chat.ehd-zr.cbr.ru"
export no_proxy="localhost,127.0.0.1,10.1.5.97,10.1.5.6,10.1.26.2,chat.ehd-zr.cbr.ru"
