# macOS localization glossary

How the macOS app's catalogs are written in `en`, `de`, `nl`, `fr`, `es`, `it`, `pt`, `ru`, Simplified Chinese (`zh-Hans`) and Lithuanian (`lt`). It is an editorial guide, not a search-and-replace dictionary: keep the meaning of each operation, the difference between an action and a status, and the difference between an application and the service or storage it manages.

Contents:

- [Sources and precedence](#sources-and-precedence)
- [Names and tokens that stay as they are](#names-and-tokens-that-stay-as-they-are)
- [Apple names](#apple-names)
- [App labels](#app-labels)
- [Vocabulary](#vocabulary)
- [Writing rules](#writing-rules)
- [Language notes](#language-notes)
- [Error lines and recovery guidance](#error-lines-and-recovery-guidance)
- [Summary keys](#summary-keys)
- [Plurals](#plurals)
- [Wording decisions](#wording-decisions)
- [Checking a change](#checking-a-change)

## Sources and precedence

1. Wording the maintainer approved takes precedence over style preferences. The six settings descriptions approved on 2026-09-22 remain the English source; report a factual concern separately instead of silently changing their claims.
2. Names of macOS applications, settings and buttons follow Apple's documentation for that language. These sources settle terminology, not whether this app behaves correctly.
3. Our own UI uses natural technical language. The translations below are project choices unless they are marked as Apple names.
4. Literal program names, file names, paths, flags, environment variables, API fields, device identifiers and user-supplied names stay as they are. A translated label must not suggest that a file or program was renamed.
5. Portuguese is Brazilian Portuguese (see [Portuguese](#portuguese)). That is a wording choice only: do not change locale routing or claim that `pt` is `pt-BR`.
6. A cell may hold two terms when one English word covers two concepts, written `term (context) / term (context)`. Pick by context; a split cell is a decision, not an inconsistency to clean up.
7. The Lithuanian column follows the human translation contributed in [issue #195](https://github.com/jamesyc/TimeCapsuleSMB/issues/195) (commit `e3392bd5`), except where [Lithuanian](#lithuanian) says otherwise.

## Names and tokens that stay as they are

Keep `TimeCapsuleSMB`, `Samba`, `Netatalk`, `Bonjour`, `NetBSD`, `macOS`, `AirPort`, `AirPort Extreme`, `AirPort Express`, `SMB`, `SMB1`, `SMB2/3`, `AFP`, `SSH`, `ACP`, `NBNS`, `NetBIOS`, `DNS`, `DHCP`, `TCP`, `UDP`, `IPv4`, `IPv6`, `HFS`, `APFS`, `ACL`, `ATA`, `API`, `URL`, `UUID` and `SHA-256` recognizable and correctly capitalized. `VPN`, `WAN` and `LAN` stay as they are.

Keep literal `ACPd`, `rsync`, `fsck`, `smbd`, `wcifsnd`, `mDNSResponder`, `vfs_aio_fork`, `ShareRoot`, `rc.local`, `xattr.tdb`, `.env` and command and parameter spellings unchanged, and translate the verbs around them: Dutch `fsck uitvoeren`, French `Exécuter fsck`, Chinese `运行 fsck`. `Doctor` in prose is this project's diagnostic checks, the Checkup; the CLI command `doctor` stays literal.

Preserve `%@`, `%d`, `%lld`, positional specifiers, `%%`, URLs, backticks and intentional `\n` escapes. A translation that needs its arguments in another order uses positional specifiers, checked with a rendered example; never swap bare placeholders. Literal backend status codes in raw logs stay untranslated; the labels in the app's own status summary are translated.

## Apple names

Apple names are localized where users know a local name:

- **Apple:** `Apple` in the European languages. Chinese prose may say **苹果**, as the maintainer asked; exact compound product names, source titles, URLs and identifiers keep `Apple`. Never translate the company to Russian `Яблоко`.
- **Time Capsule:** keep **Time Capsule** for choosing and identifying the hardware. Apple's Chinese documentation also calls it **时间返回舱**; `Time Capsule（时间返回舱）` can explain it on a first mention. Do not rename the project `TimeCapsuleSMB` or a discovered device. A sentence that can be about any supported AirPort, an AirPort Extreme with a USB disk included, says *the device* (*das Gerät*, *l’appareil*, *el dispositivo*, *设备*, *įrenginys*), not Time Capsule. ([Chinese AirPort guide](https://support.apple.com/zh-cn/guide/aputility/welcome/mac))
- **Time Machine:** **时间机器** in Chinese prose; **Time Machine** in the other languages and in identifiers. ([Chinese backup guide](https://support.apple.com/zh-cn/104984))
- **Finder:** **访达** in Chinese; **Finder** elsewhere. ([Chinese guide naming Finder and Disk Utility](https://support.apple.com/zh-cn/102184))

| Language | Keychain (the store) | Keychain Access | Disk Utility | System Settings | AirPort Utility |
|---|---|---|---|---|---|
| English | keychain | Keychain Access | Disk Utility | System Settings | AirPort Utility |
| German | Schlüsselbund | [Schlüsselbundverwaltung](https://support.apple.com/de-de/guide/keychain-access/welcome/mac) | [Festplattendienstprogramm](https://support.apple.com/de-de/guide/disk-utility/welcome/mac) | [Systemeinstellungen](https://support.apple.com/de-de/guide/mac-help/mh15217/mac) | [AirPort-Dienstprogramm](https://support.apple.com/de-de/guide/aputility/welcome/mac) |
| Dutch | sleutelhanger | [Sleutelhangertoegang](https://support.apple.com/nl-nl/guide/keychain-access/welcome/mac) | [Schijfhulpprogramma](https://support.apple.com/nl-nl/guide/disk-utility/welcome/mac) | [Systeeminstellingen](https://support.apple.com/nl-nl/guide/mac-help/mh15217/mac) | [AirPort-configuratieprogramma](https://support.apple.com/nl-nl/guide/aputility/welcome/mac) |
| French | trousseau | [Trousseaux d’accès](https://support.apple.com/fr-fr/guide/keychain-access/welcome/mac) | [Utilitaire de disque](https://support.apple.com/fr-fr/guide/disk-utility/welcome/mac) | [Réglages Système](https://support.apple.com/fr-fr/guide/mac-help/mh15217/mac) | [Utilitaire AirPort](https://support.apple.com/fr-fr/guide/aputility/welcome/mac) |
| Spanish | llavero | [Acceso a Llaveros](https://support.apple.com/es-es/guide/keychain-access/welcome/mac) | [Utilidad de Discos](https://support.apple.com/es-es/guide/disk-utility/welcome/mac) | [Ajustes del Sistema](https://support.apple.com/es-es/guide/mac-help/mh15217/mac) | [Utilidad AirPort](https://support.apple.com/es-es/guide/aputility/aprtc6ff2ed9/mac) |
| Italian | portachiavi | [Accesso Portachiavi](https://support.apple.com/it-it/guide/keychain-access/welcome/mac) | [Utility Disco](https://support.apple.com/it-it/guide/disk-utility/welcome/mac) | [Impostazioni di Sistema](https://support.apple.com/it-it/guide/mac-help/mh15217/mac) | [Utility AirPort](https://support.apple.com/it-it/guide/aputility/aprtc6ff2ed9/mac) |
| Portuguese (Brazil) | chaves | [Acesso às Chaves](https://support.apple.com/pt-br/guide/keychain-access/welcome/mac) | [Utilitário de Disco](https://support.apple.com/pt-br/guide/disk-utility/welcome/mac) | [Ajustes do Sistema](https://support.apple.com/pt-br/guide/mac-help/mh15217/mac) | [Utilitário AirPort](https://support.apple.com/pt-br/guide/aputility/-aprtc6ff2ed9/mac) |
| Russian | связка ключей | [Связка ключей](https://support.apple.com/ru-ru/guide/keychain-access/welcome/mac) | [Дисковая утилита](https://support.apple.com/ru-ru/guide/disk-utility/welcome/mac) | [Системные настройки](https://support.apple.com/ru-ru/guide/mac-help/mh15217/mac) | [Утилита AirPort](https://support.apple.com/ru-ru/guide/aputility/aprtc6ff2ed9/mac) |
| Simplified Chinese | 钥匙串 | [钥匙串访问](https://support.apple.com/zh-cn/guide/keychain-access/welcome/mac) | [磁盘工具](https://support.apple.com/zh-cn/guide/disk-utility/dskutl1027/mac) | [系统设置](https://support.apple.com/zh-cn/guide/mac-help/mchldfdf4f86/mac) | [AirPort 实用工具](https://support.apple.com/zh-cn/guide/aputility/welcome/mac) |
| Lithuanian | Keychain | Keychain Access | Disk Utility | sistemos nustatymai | AirPort Utility |

- "Cannot read the password from Keychain" is about the **store**, not about opening Keychain Access; do not turn every storage error into an application error. The newer Passwords app does not replace the keychain API's name.
- Chinese also uses Apple's [终端](https://support.apple.com/zh-cn/guide/terminal/welcome/mac) for Terminal.
- The German and French AirPort Utility names are also in Apple's [German](https://support.apple.com/de-de/112560) and [French](https://support.apple.com/fr-fr/112560) application inventories, which are evidence for the names only, not for current OS compatibility.
- These are external applications; they do not decide the name of this app's own Settings screen. Spanish OS names follow Apple's Spain documentation; if the product switches to Latin American Spanish, check those names against that locale.
- A path through an OS application names each step by Apple's current label, quoted like any other label and joined with ` > `: `Open “System Settings” > “Privacy & Security” > “Local Network”.`, `Öffnen Sie „Systemeinstellungen“ > „Datenschutz & Sicherheit“ > „Lokales Netzwerk“.` Apple's German, Dutch, Russian and Chinese help quotes every step the same way; its French, Spanish and Italian help quotes only the sidebar item and its Portuguese help none, but the catalog quotes every label alike. Current pane names include Italian *Privacy e Sicurezza* and Chinese 隐私与安全. Lithuanian keeps Apple's English names, unquoted (`Privacy & Security > Local Network`).

## App labels

The labels of this app's own buttons, screens and operations. Copy a label from its key in `Localizable.strings` rather than from here; the keys are the source and these tables only record them.

| Label | de | nl | fr | es |
|---|---|---|---|---|
| Install / Update Samba (button) | Samba installieren / aktualisieren | Samba installeren / bijwerken | Installer / mettre à jour Samba | Instalar / actualizar Samba |
| Install / Update (tab, timeline) | Installieren / Aktualisieren | Installeren / bijwerken | Installer / mettre à jour | Instalar / actualizar |
| Activate (NetBSD 4) | Aktivieren | Activeren | Activer | Activar |
| Checkup (screen and operation) | Überprüfung | Controle | Vérification | Comprobación |
| Disk Repair | Festplattenreparatur | Schijfreparatie | Réparation de disque | Reparación de disco |
| Uninstall | Deinstallieren | Verwijderen | Désinstaller | Desinstalar |
| Forget | Vergessen | Vergeten | Oublier | Olvidar |
| Remove From This Mac (Forget's menu item) | Von diesem Mac entfernen | Op deze Mac vergeten | Supprimer de ce Mac | Quitar de este Mac |
| Diagnostics (screen) | Diagnose | Diagnostiek | Diagnostic | Diagnóstico |
| Copy Diagnostics | Diagnosedaten kopieren | Diagnostische gegevens kopiëren | Copier le diagnostic | Copiar diagnóstico |
| Save Profile | Profil speichern | Profiel opslaan | Enregistrer le profil | Guardar perfil |
| Checkup section: the device's own checks | Gerät | Apparaat | Appareil | Dispositivo |
| Checkup section: this Mac's checks (the CLI says "This computer") | Dieser Mac | Deze Mac | Ce Mac | Este Mac |
| Checkup status: nothing ran on the device | Nicht geprüft | Niet gecontroleerd | Non vérifié | No comprobado |

| Label | it | pt-BR | ru | zh-Hans | lt |
|---|---|---|---|---|---|
| Install / Update Samba (button) | Installa/aggiorna Samba | Instalar/atualizar Samba | Установить/обновить Samba | 安装 / 更新 Samba | Įdiegti / atnaujinti Samba |
| Install / Update (tab, timeline) | Installa/aggiorna | Instalar/atualizar | Установить/обновить | 安装 / 更新 | Diegti / atnaujinti |
| Activate (NetBSD 4) | Attiva | Ativar | Активировать | 启动 SMB | Aktyvuoti |
| Checkup (screen and operation) | Controllo | Verificação | Проверка | 检查 | Patikrinimas |
| Disk Repair | Riparazione disco | Reparo de disco | Исправление диска | 磁盘修复 | Disko taisymas |
| Uninstall | Disinstalla | Desinstalar | Удалить | 卸载 | Pašalinti (button) / Pašalinimas (operation) |
| Forget | Dimentica | Esquecer | Забыть | 忘记 | Pamiršti |
| Remove From This Mac (Forget's menu item) | Rimuovi da questo Mac | Remover deste Mac | Убрать с этого Mac | 从此 Mac 移除 | Pamiršti šiame Mac |
| Diagnostics (screen) | Diagnostica | Diagnóstico | Диагностика | 诊断 | Diagnostika |
| Copy Diagnostics | Copia diagnostica | Copiar diagnóstico | Скопировать диагностику | 复制诊断信息 | Kopijuoti diagnostiką |
| Save Profile | Salva profilo | Salvar perfil | Сохранить профиль | 保存配置 | Išsaugoti profilį |
| Checkup section: the device's own checks | Dispositivo | Dispositivo | Устройство | 设备 | Įrenginys |
| Checkup section: this Mac's checks (the CLI says "This computer") | Questo Mac | Este Mac | Этот Mac | 此 Mac | Šis Mac |
| Checkup status: nothing ran on the device | Non controllato | Não verificado | Не проверено | 未检查 | Nepatikrinta |

- **Activate** starts the installed Samba on a NetBSD 4 device; on a device without the boot hook it lasts until the next reboot. Spanish and Italian keep *activar*/*attivare* for Activate alone, because they enable with *habilitar*/*abilitare*, and Dutch, Russian and Lithuanian enable with *inschakelen*, *включить* and *įjungti*. German, French and Portuguese use the same verb for both (`Aktivieren` / `SSH aktivieren`, `Activer` / `Activer SSH`, `Ativar` / `Ativar SSH`). That overlap is accepted: Activate is always a button about Samba, and enabling always names what it enables. Do not rename Activate to avoid it. Chinese names what starts (启动 SMB; see [Chinese](#chinese)).
- **Disk Repair** is this app's fsck operation, not an Apple application, so every language translates it, Lithuanian included (*Disko taisymas*, never `Disk Repair`). Its steps (`timeline.fsck.phase.*`) are the HFS structures `fsck_hfs` checks, written like the locale's other progress lines (German passive, Dutch infinitive, French, Italian and Russian nouns, Spanish and Portuguese gerunds, Chinese 正在…, Lithuanian passive participles). Name the structure instead of quoting `fsck_hfs`'s English (`Checking Catalog file.`): the catalog file, the extents overflow file, the volume bitmap, the extended attributes file. The volume is the one being repaired (German *Datenträger*, Lithuanian *tomas*); the disk is the hardware (*Festplatte*, *diskas*). After a repair `fsck_hfs` checks everything again, possibly more than once; those steps say "(recheck)" (*erneute Prüfung*, *nouvelle vérification*, 重新检查), never a numbered pass.
- **Uninstall** deletes TimeCapsuleSMB from the device. **Forget** and its menu item **Remove From This Mac** only delete the saved device from this Mac; they never uninstall anything. Forget's menu item and the question it asks never use the uninstall verb: where that verb is also a language's everyday "remove" (Dutch *verwijderen*, Russian *удалить*, Lithuanian *pašalinti*), they say *vergeten*, *убрать* and *pamiršti*. A sentence that names Forget may still say what it removes in ordinary words (`verwijder dubbele vermeldingen met ‘Vergeten’`).
- **Diagnostics** is the screen; **Copy Diagnostics** copies its report. German, Dutch and Chinese name the copied report as data (*Diagnosedaten*, *diagnostische gegevens*, 诊断信息); that is the button's wording, not a second name for the screen.
- One action has one spelling in a locale, slash spacing included, across every key that shows it: buttons, tab titles, timeline names, confirmation titles and the sentences that name it. When a label changes, search the catalog for its old spelling and change every mention in the same commit.
- In an action name made of two verbs joined by a slash, the verb after the slash is lowercase (`Installer / mettre à jour Samba`, `Instalar / actualizar Samba`, `Installa/aggiorna Samba`), except where the language capitalizes it anyway (German nominalized infinitives: `Installieren / Aktualisieren`).

## Vocabulary

Base forms; inflect them naturally rather than splicing them into sentences.

| Concept | de | nl | fr | es |
|---|---|---|---|---|
| Running | Wird ausgeführt (operation) / läuft (service) | Bezig (operation) / actief (service) | En cours d’exécution | En ejecución |
| Stop (Samba, file sharing, a service) | beenden (Samba, file sharing) / stoppen (managed services) | stoppen | arrêter | detener |
| Passed check | Bestanden | Geslaagd | Réussi | Aprobado |
| Warning | Warnung | Waarschuwing | Avertissement | Advertencia |
| Failed | Fehlgeschlagen (label) / ist fehlgeschlagen (sentence) | Mislukt (label) / is mislukt (sentence) | Échec (label) / a échoué (sentence) | Error (label) / falló (sentence) |
| Idle | Leerlauf | Inactief (status) / inactiviteit (ATA timer) | Inactif (status) / inactivité (ATA timer) | Inactivo (status) / inactividad (ATA timer) |
| Standby | Standby | stand-by | veille | modo de espera |
| Reachable | erreichbar | bereikbaar | accessible | accesible |
| Enable / disable | aktivieren / deaktivieren | inschakelen / uitschakelen | activer / désactiver | habilitar / deshabilitar |
| Reboot / restart | Neustart (noun) / neu starten (verb) | herstart (noun) / opnieuw opstarten (verb) | redémarrage (noun) / redémarrer (verb) | reinicio (noun) / reiniciar (verb) |
| Unplug (cut the device's power) | vom Strom trennen | de stekker eruit halen | débrancher | desenchufar |
| Device profile | Geräteprofil | apparaatprofiel | profil de l’appareil | perfil del dispositivo |
| Helper program | Hilfsprogramm | hulpprogramma | programme auxiliaire | programa auxiliar |
| Discovery | Erkennung (process) / Gerätesuche (operation title) | detectie (process) / apparaatdetectie (operation title) | découverte | descubrimiento |
| Host | Host | host | hôte | host |
| Flash (NAND storage) | Flash-Speicher; Flash- in compounds | flashgeheugen | mémoire flash | memoria flash |
| Firmware | Firmware | firmware | firmware | firmware |
| Disk mount | einbinden | koppelen | monter | montar |
| Disk I/O | Festplatten-I/O | schijf-I/O | E/S disque | E/S de disco |
| Snapshot | Momentaufnahme | momentopname | instantané | instantánea |
| Permissions | Zugriffsrechte | toegangsrechten | autorisations | permisos |
| Metadata | Metadaten | metadata | métadonnées | metadatos |
| Metadata migration | Metadaten-Migration; Samba-Metadaten-Migration | metadatamigratie; Samba-metadatamigratie | migration des métadonnées (Samba) | migración de metadatos (de Samba) |
| Extended attributes | erweiterte Attribute | uitgebreide attributen | attributs étendus | atributos extendidos |
| File sharing | Dateifreigabe | bestandsdeling | partage de fichiers | uso compartido de archivos |
| Network share | Freigabe (noun) / freigeben (verb) | share (noun) / delen (verb) | partage (noun) / partager (verb) | recurso compartido (noun) / compartir (verb) |
| SMB login (signing in to a share) | SMB-Anmeldung | SMB-aanmelding | connexion SMB | inicio de sesión SMB |
| Authentication | Authentifizierung | authenticatie | authentification | autenticación |
| Encryption | Verschlüsselung | versleuteling | chiffrement | cifrado |
| Signing | Signierung | ondertekening | signature | firma |
| Firewall | Firewall | firewall | coupe-feu | cortafuegos |
| Security app | Sicherheitssoftware | beveiligingssoftware | logiciel de sécurité | software de seguridad |
| Internet (WAN) side | Internetanschluss (WAN) | internetkant (WAN) | côté Internet (WAN) | lado de Internet (WAN) |
| Guest network | Gastnetzwerk | gastnetwerk | réseau invité | red de invitados |
| Main network | Hauptnetzwerk | hoofdnetwerk | réseau principal | red principal |
| LAN port | LAN-Anschluss | LAN-poort | port LAN | puerto LAN |
| Wi-Fi / Ethernet | Wi-Fi / Ethernet | Wi-Fi / Ethernet | Wi-Fi / Ethernet | Wi-Fi / Ethernet |

| Concept | it | pt-BR | ru | zh-Hans | lt |
|---|---|---|---|---|---|
| Running | In esecuzione | Em execução | Выполняется (operation) / работает (service) | 运行中 | Vykdoma (operation) / veikia (service) |
| Stop (Samba, file sharing, a service) | arrestare | interromper | остановить | 停止 | sustabdyti |
| Passed check | Superato | Aprovado | Пройдено | 通过 | Pavyko |
| Warning | Avviso | Aviso | Предупреждение | 警告 | Įspėjimas |
| Failed | Non riuscito (label) / non è riuscito (sentence) | Falha (label) / falhou (sentence) | Ошибка (label) / не удалось (sentence) | 失败 | Nepavyko |
| Idle | Inattivo (status) / inattività (ATA timer) | Inativo (status) / inatividade (ATA timer) | В режиме ожидания (status) / простой (ATA timer) | 空闲 | Laukiama (status) / neveikla (ATA timer) |
| Standby | standby | modo de espera | режим ожидания | 待机 | budėjimas |
| Reachable | raggiungibile | acessível | доступен | 可达 (network status sentences) / 可访问 (short status value) | pasiekiamas |
| Enable / disable | abilitare / disabilitare | ativar / desativar | включить / отключить | 启用 / 禁用 (a feature or setting) / 停用 (other software the user runs) | įjungti / išjungti |
| Reboot / restart | riavvio (noun) / riavviare (verb) | reinicialização (noun) / reiniciar (verb) | перезагрузка (noun) / перезагрузить (verb) | 重启 | paleidimas iš naujo (noun) / paleisti iš naujo (verb) |
| Unplug (cut the device's power) | scollegare dall’alimentazione | desconectar da tomada | отключить от питания | 拔掉电源 | atjungti nuo maitinimo |
| Device profile | profilo del dispositivo | perfil do dispositivo | профиль устройства | 设备配置 | įrenginio profilis |
| Helper program | programma ausiliario | programa auxiliar | вспомогательная программа | 辅助程序 | pagalbinė programa |
| Discovery | rilevamento | descoberta | обнаружение | 发现 | paieška |
| Host | host | host | хост | 主机 | kompiuteris; kompiuterio pavadinimas (a name to type) |
| Flash (NAND storage) | memoria flash | memória flash | флеш-память | 闪存 | flash atmintis |
| Firmware | firmware | firmware | прошивка | 固件 | programinė įranga |
| Disk mount | montare | montar | монтировать | 挂载 | prijungti |
| Disk I/O | I/O del disco | E/S de disco | дисковый ввод-вывод | 磁盘 I/O | disko įvestis ir išvestis |
| Snapshot | istantanea | snapshot | снимок | 快照 | momentinė kopija |
| Permissions | permessi | permissões | права доступа | 权限 | leidimai |
| Metadata | metadati | metadados | метаданные | 元数据 | metaduomenys |
| Metadata migration | migrazione dei metadati (Samba) | migração de metadados (do Samba) | перенос метаданных (Samba) | (Samba) 元数据迁移 | (Samba) metaduomenų perkėlimas |
| Extended attributes | attributi estesi | atributos estendidos | расширенные атрибуты | 扩展属性 | išplėstiniai atributai |
| File sharing | condivisione dei file | compartilhamento de arquivos | общий доступ к файлам | 文件共享 | failų bendrinimas |
| Network share | condivisione (noun) / condividere (verb) | compartilhamento (noun) / compartilhar (verb) | общий ресурс (noun) / открыть общий доступ (verb) | 共享 | bendrinamas diskas (noun) / bendrinti (verb) |
| SMB login (signing in to a share) | accesso SMB | login SMB | вход по SMB | SMB 登录 | SMB prisijungimas |
| Authentication | autenticazione | autenticação | аутентификация | 身份验证 | autentifikacija |
| Encryption | crittografia | criptografia | шифрование | 加密 | šifravimas |
| Signing | firma | assinatura | подпись | 签名 | pasirašymas |
| Firewall | firewall | firewall | брандмауэр | 防火墙 | užkarda |
| Security app | software di sicurezza | software de segurança | защитное ПО | 安全软件 | saugos programa |
| Internet (WAN) side | lato Internet (WAN) | lado da internet (WAN) | сторона интернета (WAN) | 互联网（WAN）一侧 | interneto (WAN) pusė |
| Guest network | rete ospiti | rede de convidados | гостевая сеть | 访客网络 | svečių tinklas |
| Main network | rete principale | rede principal | основная сеть | 主网络 | pagrindinis tinklas |
| LAN port | porta LAN | porta LAN | порт LAN | LAN 端口 | LAN prievadas |
| Wi-Fi / Ethernet | Wi-Fi / Ethernet | Wi-Fi / Ethernet | Wi-Fi / Ethernet | Wi-Fi / 以太网 | Wi-Fi / Ethernet |

### Notes on the vocabulary

- **Stop:** stopping something (Samba, file sharing, the managed services). Something that stops by itself uses the ordinary verb (Portuguese *parou*, Italian *si è fermato*, Lithuanian *sustojo*). A metadata migration that ended on an error *failed*, it did not stop; see [Error lines](#error-lines-and-recovery-guidance).
- **Failed:** the sentence form agrees with its noun (Italian *la migrazione … non è riuscita*, Russian *перенос … не удался*).
- **Enable / disable:** one pair per feature everywhere it appears (setting label, confirmation, result summary). It is Apple's enable verb in each language. Spanish and Italian enable with *habilitar*/*abilitare* and keep *activar*/*attivare* for **Activate**; in German, French and Portuguese the two share a verb (see [App labels](#app-labels)).
- **Reachable:** a network service or host answers on its port. It is not "available" (usable), "accessible" in the disability sense, or "connected".
- **Reboot / restart:** the whole device starting over, as the AirPort reboot request does. English uses both words for one thing; translate both with the row's term. Chinese says 重启 (or 重新启动), never a bare 启动, which is **Activate**.
- **Unplug:** take the device off its power to restart it, when a restart request does not work. Say which plug: *vom Strom trennen*, *de stekker eruit halen*, *desenchufar* (or *desconectar la alimentación*), *desconectar da tomada*, *отключить от питания*, *拔掉电源*, *atjungti nuo maitinimo*, not a bare *trennen*, *loskoppelen*, *desconectar* or *отключить*, which can read as unplugging a cable or disconnecting from the network. Older lines that also say to plug it back in (the flash power-cycle steps, the stuck-service error) use the bare forms, which that makes clear.
- **Host:** a network host, entered as a name or IP address; not a "server" in the client/server sense, and not the person hosting. An SSH or SMB server is a server (Chinese 服务器, Lithuanian *serveris*).
- **Flash:** the router's NAND flash storage, or writing to it; never a camera flash or Adobe Flash.
- **Disk mount:** Chinese 挂载 is our general term even though [Disk Utility's button says 装载](https://support.apple.com/zh-cn/guide/disk-utility/dskutl1027/mac) and [uses 卸载](https://support.apple.com/zh-cn/guide/disk-utility/dskud709f49b/mac); quote Disk Utility's own words when giving Disk Utility instructions.
- **Metadata migration:** the deploy step that moves an earlier version's Samba metadata (`xattr.tdb`) onto the files. Name Samba where the English does. German and Dutch keep the compound (*Samba-Metadaten-Migration*, *Samba-metadatamigratie*), not *Migration der Samba-Metadaten*. "Migrated" follows the noun: German *migriert*, Dutch *gemigreerd*, Russian *перенесенные*, Lithuanian *perkelti*, Chinese 迁移.
- **File sharing and network share:** file sharing is the service; a share is one shared disk. Lithuanian names a share *bendrinamas diskas*, not *bendrinimas*, which is the act of sharing.
- **Authentication:** Chinese 身份验证 is the usual macOS term for checking a password or key. 双重认证 is only Apple's two-factor brand name; do not use 认证 or 身份认证 for SSH or ACP.
- **Firewall:** Apple's word in each language's "Block connections to your Mac with a firewall" page ([fr](https://support.apple.com/fr-fr/guide/mac-help/mh34041/mac), [es](https://support.apple.com/es-es/guide/mac-help/mh34041/mac), [ru](https://support.apple.com/ru-ru/guide/mac-help/mh34041/mac), [zh-cn](https://support.apple.com/zh-cn/guide/mac-help/mh34041/mac)): French *coupe-feu*, not *pare-feu*. Lithuanian has no Apple localization and uses the common *užkarda*.
- **Security app:** other software on the Mac that filters or routes its network traffic, named next to VPN and firewall. Lithuanian says *saugos programa*, not *saugos programinė įranga*, which reads as firmware.
- **Local Network access:** the macOS privacy permission. Prose says it in ordinary words, as the card titles do (*Zugriff auf das lokale Netzwerk*, *toegang tot het lokale netwerk*, *accès au réseau local*, *acceso a la red local*, *accesso alla rete locale*, *acesso à rede local*, *доступ к локальной сети*, 本地网络访问权限, *vietinio tinklo prieiga*); only a settings path quotes the pane's name. Turning the app on or off in that pane uses the row's enable/disable pair.
- **Other copies of the app:** copies of TimeCapsuleSMB on this Mac, deleted by the user; never the **Uninstall** operation. Dutch and Russian, whose Uninstall label is their general delete verb (*Verwijderen*, *Удалить*), say move them to the Trash (*naar de prullenmand*, *в Корзину*).
- **Internet (WAN) side, guest network, main network, LAN port:** the networks of an AirPort in router mode. The internet side is the network on its WAN port; it shares disks there and on its guest network only with "Share disks over WAN". Its main network is the one on its LAN ports and main Wi-Fi. German says *Internetanschluss*, never *Internetseite* (a web page).
- **Wi-Fi and Ethernet:** `Wi-Fi` in every language; Apple's German documentation says *WLAN*, but the catalog keeps *Wi-Fi*. `Ethernet` everywhere except Chinese, which uses Apple's **以太网**.
- **Borrowings:** Dutch `share`, Italian `log` and Portuguese `snapshot` are fine in this app; do not replace them with awkward literal inventions. `Profile`, `Diagnostics`, `running` and `bundled` are not protected names.

### Words with one meaning here

| Source term | Decision |
|---|---|
| payload | Deployment files: "installation files" or "Samba files". A JSON response: "response data". A firmware image segment: keep the distinction ("firmware payload"), never a complete firmware image. Lithuanian: *diegimo failai* / *diegimo tomas* / *diegimo aplankas* for deployment, *programinės įrangos turinys* for the firmware segment, never *apkrova* (a physical load). |
| runtime | Service status: "services" or "service status". A technical execution environment: "runtime environment". Do not turn every running process into an abstract "runtime". |
| backend | Progress: "background operation". The app/helper architecture or raw events: `backend`. Lithuanian *foninė operacija* / *pagalbinė programa*, never *serveris* (the helper runs on the Mac). Russian *фоновая операция* / *вспомогательная программа*, never *фоновая служба* (a service on the device). Chinese 后台操作 / 后端 (raw events only). |
| boot hook | A startup hook or startup mechanism; specialist firmware screens may add `boot hook` in parentheses. Not the bootloader. |
| firmware bank | A firmware storage bank or slot; not a financial bank, a disk partition or a memory bank. Keep primary/inactive/active distinct. |
| advertise | Announce a network service: German *ankündigen*, Dutch *aankondigen*, French *annoncer*, Spanish *anunciar*, Italian *annunciare*, Portuguese *anunciar*, Russian *объявлять*, Chinese 广播. Never a commercial advertising verb. |
| idle | An inactive or waiting status. The ATA idle timer and ATA standby timer are separate settings; never one sleep timer. |
| root | Filesystem root: the local term. The Unix account: literal `root`. A root certificate: the certificate term. `ShareRoot`: literal. |
| plan / dry run | A preview of actions, not running or activating a "plan". Keep planning, confirming and writing distinct. |
| repair / restore / reset | Repair damage; restore an earlier or original state; reset editable values. Never one verb for all three. |
| pass / fail | A test result; never walking past, handing over or running. Localize result labels; keep machine-readable status codes. |
| check, plan, directory, flush, settle | A test, not a bank cheque; an action plan, not a subscription; a folder, not a phone book; complete pending writes, not erase the disk; let startup stabilize, not pay a bill. |

## Writing rules

### Sentences and meaning

- Translate complete sentences. Keeping a technical word does not excuse broken word order or missing agreement.
- Keep the meaning and severity of every error, above all "not", "only", "before", "after", "may" and "must". "May help" never becomes a guarantee, and a possible cause ("the disk may be failing") never becomes a diagnosis.
- Do not drop technical detail to shorten a warning, and do not add behavioral claims while translating.
- Use sentence case, except for nouns a language capitalizes and exact OS and product names. Do not carry English title case over.
- A button is an action verb; a status is a state or result. Keep one voice per locale: German and Russian buttons use infinitives, Dutch buttons normally put the verb last. Do not mix polite commands, infinitives and noun phrases at random.

### Naming buttons, screens and operations

- A sentence that names a button, screen or operation by its label quotes the label exactly as the key spells it, spacing and capitalization included: run "Disk Repair", open "Diagnostics", "Install / Update Samba" waits, use "Forget". This holds in every language, English included, and in error lines, recovery guidance and prose alike. Each locale uses its own quotation marks (see [Punctuation](#punctuation)); the helper's CLI text uses straight quotes (`run "Install / Update Samba" in the macOS app`). A helper message with no catalog key reaches the app in English, where some titles differ from the CLI's (the CLI's section `This computer` is the app's `This Mac`), so such a message describes the section (`see this computer's checks`) instead of quoting a title.
- A title, button or status line that names its own operation is that operation's label, unquoted (`Run Disk Repair?`, `Disk Repair Complete`, `Install / Update failed.`).
- A sentence that describes the work in ordinary words uses the ordinary noun, unquoted: `Disk repair completed with fsck.`, `The checkup could not complete.`, `Die Deinstallation entfernt verwaltete Dateien`, `Pašalinimas ištrina…`. Do not mix the two by writing a label unquoted.
- A label keeps its exact form inside the quotes. Where the sentence would change its case, Russian and Lithuanian put it after a noun, where it stays nominative (Russian `запустите операцию «Проверка»`, `откройте раздел «Диагностика»`; Lithuanian `paleiskite operaciją „Disko taisymas“`, `atidarykite skiltį „Diagnostika“`).
- German and Dutch close a split verb after its last object, so each label in a sequence keeps its verb: `Führen Sie die „Festplattenreparatur“ für die Festplatte und danach erneut „Samba installieren / aktualisieren“ aus.`, `voer ‘Schijfreparatie’ uit en voer daarna ‘Samba installeren / bijwerken’ opnieuw uit`.
- Copy each label from its key; retyped labels drift from the button.
- The approved NetBSD 4 notices that say "may need Activate" keep that wording (see [Wording decisions](#wording-decisions)).

### Form of address

| de | nl | fr | es | it | pt-BR | ru | zh-Hans | lt |
|---|---|---|---|---|---|---|---|---|
| Sie | u | vous | usted | tu | você | вы | 你 | jūs |

Commands use the matching form everywhere (Spanish *ejecute*, *vuelva a intentarlo*, never *ejecuta*, *vuelve*; Lithuanian *paleiskite*). Chinese and Dutch often need no pronoun at all: Chinese leaves it out where the sentence reads naturally (`文件仍保留在磁盘上。`, not `您的文件仍保留在磁盘上。`), and Dutch commands are imperatives (`Klik op ‘Profiel opslaan’`).

### Punctuation

| Locale | Quotation marks | Apostrophe | Ellipsis |
|---|---|---|---|
| en | `“…”` | `'` | `...` |
| de, lt | `„…“` | (none in use) | `...` |
| nl | `‘…’` | `'` | `...` |
| fr | `« … »` with no-break spaces | `’` | `...` |
| es, ru | `«…»` | (none in use) | `...` |
| it, pt | `“…”` | `’` in Italian | `...` |
| zh-Hans | `“…”` | (none in use) | `……` in prose, `…` after a button label |

- One kind of quotation mark per locale, everywhere in its catalog. English never writes straight quotes (`\"…\"`). Dutch uses single quotes because Apple's Dutch documentation does (`Kies Apple-menu > 'Systeeminstellingen'`). Spanish uses `«…»`, the Real Academia Española's first choice, although Apple's Spanish documentation uses `“…”`.
- French puts a no-break space (U+00A0) before `?`, `!`, `:` and `;` and inside `« »`, so a mark never starts or ends a line alone: `Continuer ?`, `app : %@`.
- French and Italian write the typographic apostrophe (`l’appareil`, `dell’app`), never `'`. English and Dutch write `'`.
- Ellipses follow the English three periods. Chinese writes `……` where a sentence trails off (`正在查找设备……`) and Apple's `…` after a label that opens a dialog (`保存诊断信息…`).
- Spanish questions open with `¿`.
- Chinese uses full-width `，：；（）`, parentheses around Latin text included (`SMB 3（protocol_vers_map）`, `互联网（WAN）一侧`), and puts a space between Chinese and Latin text or numbers outside them (`Samba 元数据迁移`, `等待 10 秒`).

## Language notes

Rules that apply to one language. The vocabulary tables above cover single terms.

### German

- *Festplatte* is the physical disk: it fails, spins up, is tested, repaired or replaced (*Festplattenreparatur*). *Datenträger* is a volume that is found, mounted or unmounted (`HFS-Datenträger`, `Datenträger nicht eingebunden`).
- A disk that may be failing *fällt möglicherweise aus*.
- A migration that failed *ist fehlgeschlagen*; *angehalten* reads as paused.

### Dutch

- *reparatie* is repair and *herstel* is restore (`Herstel naar flash`).
- A disk that may be failing *is mogelijk defect*.
- Uninstall is *Verwijderen*, so Forget's menu item and question say *vergeten* (`Op deze Mac vergeten`, `%@ op deze Mac vergeten?`); like other menu items, the verb comes last.

### French

- *Paramètres* is this app's own settings; *Réglages* is only Apple's *Réglages Système*.
- *lancer* starts an operation of this app, in buttons and sentences alike (`Lancer la vérification`, `Lancez « Réparation de disque »`, `relancez « Installer / mettre à jour Samba »`); *exécuter* runs a command, a program or a plan (`Exécuter fsck`, `exécuter TimeCapsuleSMB`, and the CLI's `activate`); a running operation is *en cours d’exécution*.
- A disk that may be failing *est peut-être défaillant*.

### Spanish

- The Mac is masculine, as in Apple's Spain documentation: *este Mac*, *de este Mac*. Checkup is *Comprobación*, also Spain's word; *Chequeo* is Latin American.
- A sentence about what an operation does names it as the subject (`La reparación reinicia el dispositivo cuando termina.`): a bare third person (`Reinicia el dispositivo`) is also the *tú* command and reads as an instruction.
- A disk that may be failing *puede estar fallando*.
- The catalog never settled a gender for Time Capsule, so a sentence puts it after *dispositivo* (*el dispositivo Time Capsule*).
- Waking a sleeping disk is *sacar el disco del reposo* or *despertar el disco*, never *reactivar* (Activate).

### Italian

- A disk that may be failing *potrebbe essere guasto*.
- The catalog never settled a gender for Time Capsule, so a sentence puts it after *dispositivo* (*il dispositivo Time Capsule*).
- Waking a sleeping disk is *risvegliare il disco*, never *riattivare* (Activate).
- A sentence about what an operation does names it as the subject (`Al termine, la riparazione riavvia il dispositivo.`): with *tu* commands, a bare `Riavvia il dispositivo` reads as an instruction.

### Portuguese

- Brazilian forms: `arquivo`, `compartilhamento`, `configuração`, `criptografia`, `salvar` and `planejar`, never European `ficheiro`, `partilha`, `definição`, `encriptação`, `guardar` and `planear`.
- A disk that may be failing *pode estar falhando*.

### Russian

- The catalog writes е, never ё (`сохраненное устройство`, `еще`, `все еще`).
- "Requested; not waiting for X" is *Запрошено …; без ожидания X*. Avoid *X не ожидается*, which reads as "X is not expected".
- *Доступен* means reachable; it never means "saved" or "available" (`Нет сохраненных хостов для проверки.`). Being unavailable is one word, *недоступен* (`SSH недоступен на %@`); *не доступен* only follows a modifier (`SSH еще не доступен`).
- Uninstall is *Удалить*, so Forget's menu item and question say *убрать* (`Убрать с этого Mac`, `Убрать %@ с этого Mac?`).
- A disk that may be failing is *возможно, неисправен*.

### Chinese

- Address the user as 你, never 您 (see [Form of address](#form-of-address)).
- **Activate** is 启动 and names what starts, its label always `启动 SMB` (`NetBSD4 服务启动已完成。`, `正在等待受管理服务启动完成。`), because a bare 启动 reads as the device booting (`设备完成启动`). Do not switch to 激活.
- "Already enabled/disabled" keeps "already" as 已处于…状态 (`SSH 已处于禁用状态。`), so it cannot be read as the plain result `SSH 已禁用`.
- A volume is 卷 in our prose (Apple says 宗卷); neither means a whole physical disk.
- [Apple's SMB guidance](https://support.apple.com/zh-cn/102064) confirms 共享 and 元数据, and its [certificate guide](https://support.apple.com/zh-cn/guide/keychain-access/mchlp2697/mac) 数字签名. The SMB setting is `SMB 签名`, without a claim about certificate-based signatures.
- A disk that may be failing 可能出现故障.

### Lithuanian

- macOS has no Lithuanian localization, so Apple application and product names stay in English, unquoted and uninflected: `iš Keychain`, `Atidaryti Finder`, `AirPort Utility`, `Time Machine`, `Mac`. The company name is unquoted too (`Apple originalią programinę įrangą`). Generic OS concepts are translated: System Settings is *sistemos nustatymai*.
- Run an operation whose name is a noun by that name after *operaciją*, where it stays nominative (`paleiskite operaciją „Disko taisymas“`, `„Patikrinimas“`, `„Pašalinimas“`); open a screen after *skiltį* (`atidarykite skiltį „Diagnostika“`). **Install / Update Samba** and **Activate**, whose labels are verbs, are run by their labels (`paleiskite „Įdiegti / atnaujinti Samba“`).
- An infinitive label is never the subject of a sentence: use the operation's noun, unquoted (`Pašalinimas taip pat ištrina…`), or rephrase (`Paleidus „Įdiegti / atnaujinti Samba“, diegimas palauks…`).
- *programinė įranga* is firmware, as in the human translation, although it also means software in general. Our installed software is *įdiegta programinė įranga* or *diegimo failai*, so the two never collide. A firmware **patch** is *pataisymas* (`Flash atminties pataisymo planas`).
- The flash storage is *flash atmintis* (`Flash atminties atsarginė kopija`, `Įrašyti į flash atmintį nereikėjo.`). *flash* alone only describes something done to it or kept on it (`flash įrašymas`, `flash veiksmas`, `flash įskiepiai`); paths such as `/mnt/Flash` stay literal.
- "Requested" in a status line is *Paprašyta …* (`Paprašyta įjungti SSH`), never *užklaustas* ("queried").
- Uninstall's button is *Pašalinti*, so Forget's menu item and question say *pamiršti* (`Pamiršti šiame Mac`, `Pamiršti %@ šiame Mac?`).
- A disk that may be failing *gali gesti*.
- The share and flash wording above are this glossary's decisions; they replace the human translation's *bendrinimas* and bare *Flash*. *Pašalinimas*, the Uninstall operation, is the human translation's own name for it.

## Error lines and recovery guidance

An error box shows the helper's recovery guidance in the app's language, `backend.recovery.<key>.title`, `.message` and `.action.N`, then the helper's own error text in English, smaller, for diagnostics; that line is left out when the guidance already says what it says. The key is the entry's place in `src/timecapsulesmb/app/recovery.py`: `<code>`, `<operation>.<code>` or `<operation>.<code>.<stage>`. The one-line error elsewhere in the app (install status, device list, flash screen, Settings) is `backend.error.<operation>.<code>` (or `backend.error.<code>`), else the recovery title. The English catalog repeats the helper's English word for word, and every catalog has every entry with one `.action.N` per helper step; `tests/test_error_catalog.py` checks all of this.

- Guidance is always translated: the title, the message, the steps and the buttons. Diagnostics stay English: the helper's error text, the Checkup tab, the operation log and Copy Diagnostics.
- The title says what happened. The message explains it in plain words without restating the title. The steps are actions.
- Every error code the helper can raise has its own entry; the generic `remote_error` and `operation_failed` entries are only for failures the helper cannot name. Guidance avoids the helper's internal words ("helper", "operation log", "payload", MaSt).
- Each distinct cause gets its own error code and line. Do not write one line that covers several causes ("could not write") when the helper can tell them apart.
- Error lines and recovery text name the app's own operations, not CLI words: "deploy" is **Install / Update Samba**, "run doctor" is **Checkup**, "configure" is adding a device, and the Diagnostics screen is **Diagnostics**. Literal CLI commands such as `tcapsule activate` stay.
- Recovery messages carry no `%` placeholders except the slow-device message's `%@` (the device name).
- A failed install says what is true now, not when it changes: "Samba was stopped for the install and is not running now", never "until Install / Update Samba finishes". Disk Repair can start an older NetBSD 4 install again, so Samba may be running again before the next install.
- A metadata migration that ended on an error *failed*. The helper says "stopped" only of a migration a signal ended (`xattr_migration_stopped`, shown in the helper's English).
- Uninstall deletes the Samba metadata an earlier version saved that is not migrated yet; what a migration already moved stays on the files. Say "your files stay on the disk", never that Time Machine backups are kept: a backup bundle's own metadata can be among what it deletes.
- Waking a sleeping disk is not activating it (see Spanish and Italian).
- "The device would not keep it mounted for TimeCapsuleSMB" means the device's disk manager refused to hold the volume mounted for the install; it is not a mount failure.
- A failing disk (`disk_failing`): the disk reported errors it could not recover from, which the kernel logged. The cause is "probably failing", stronger than the "may be failing" above, and never a certainty: German *fällt wahrscheinlich aus*, Dutch *is waarschijnlijk defect*, French *est probablement défaillant*, Spanish *probablemente esté fallando*, Italian *è probabilmente guasto*, Portuguese *provavelmente está falhando*, Russian *вероятно, неисправен*, Chinese 很可能出现故障, Lithuanian *greičiausiai genda*. Titles state it (*Die Festplatte fällt aus*, 磁盘出现故障). The data comes first: "copy off the files you need", then replace the disk; a disk taken out of the device connects to a Mac with a SATA-to-USB adapter (*SATA-USB-Adapter*, *SATA-naar-USB-adapter*, *adaptateur SATA vers USB*, *adaptador de SATA a USB*, *adattatore da SATA a USB*, *adaptador SATA para USB*, *переходник SATA–USB*, SATA 转 USB 转接器, *SATA–USB adapteris*). Keep the sentence that AirPort Utility may still show the disk as OK, with Apple's name for AirPort Utility.
- A confirmation that warns before an operation that cannot help (the failing-disk preflight) names its button "<action> anyway", the locale's usual concessive: German *Trotzdem installieren*, Dutch *Toch installeren*, French *Installer quand même*, Spanish *Instalar de todos modos*, Italian *Installa comunque*, Portuguese *Instalar mesmo assim*, Russian *Все равно установить*, Chinese 仍然安装, Lithuanian *Vis tiek diegti*. The verb is the operation's own (install, uninstall, repair), not its full label.
- `unsupported_device` has two meanings. Configure and deploy use it for an AirPort model that cannot run TimeCapsuleSMB (an AirPort Express); flash and Activate also use it for an operation a supported device cannot do (NetBSD 6). Only `configure.unsupported_device` and `deploy.unsupported_device` may say the model cannot run TimeCapsuleSMB or advise forgetting the device; the generic `unsupported_device` stays neutral in every language.

## Summary keys

Helper results carry an English `summary` plus a stable `summary_key` and positional `summary_args` (progress logs: `message_key`, `message_args`). The app shows `backend.summary.<summary_key>` from the catalogs and falls back to the English text. The key registry is `src/timecapsulesmb/core/summaries.py`; `tests/test_summaries.py` checks every catalog against it.

- Translate each key as a **whole sentence**. Keys are never assembled from fragments; a variant such as "with a firmware version" has its own key (`flash.apple_stock_match_version`), so each language can place the version where it reads naturally.
- Placeholders match the registry's argument types and count: `%d`/`%ld`/`%lld` for integers, `%@` for strings. Reorder with positional specifiers (`%2$@ … %1$lld`), never by swapping bare placeholders. In a plain string `%d` is fine: the app checks each placeholder and passes a 32-bit value for `%d` and a 64-bit one for `%ld`/`%lld`. A plural variable is different: Foundation reads it with its declared value type, so it must be `lld`.
- Arguments are values (counts, versions, paths, product IDs), never English words. A sentence that needs a word depending on state needs another key.
- A summary is a status line: past tense or a state, with a trailing ellipsis only where the English has one (the "Waiting..." progress logs).
- Changing an English summary's shape (adding, removing or retyping an argument) needs a new key name, so older app builds never format a new sentence with the wrong arguments.
- A summary whose wording depends on a count is a plural entry (see [Plurals](#plurals)). Turning an existing key into one keeps its name, since `%#@name@` is still an integer argument.

## Plurals

A sentence whose wording depends on a count lives in `Localizable.stringsdict`, not `Localizable.strings`, in all ten languages. The plural keys are the count summaries (`discovered_devices`, `hfs_volumes_found`, `flash.apple_some_match` and its `_version` variant), `bundle_issue.distribution_artifacts_missing_count.message` and `activity.multiple_active`. Never write "(s)", "(-ų)" or "problème(s)"-style forms, and never pick one grammatical form for every count.

Foundation picks the form from the locale the app formats with, not the system language, so plural sentences follow the app's language setting (verified on macOS 2026-09-25). Its rules match the table below, including Spanish and Italian `many` for round millions.

| Language | Forms each plural variable defines | Example |
|---|---|---|
| en, de, nl | one, other | 1 device / 0 devices, 2 devices |
| es, it | one, many (round millions), other | 1 dispositivo / 2 dispositivos / 1.000.000 de dispositivos |
| fr | one (0 and 1), many (round millions), other | 0 appareil, 1 appareil / 2 appareils / 1 000 000 d’appareils |
| pt-BR | zero, one, many (round millions), other | 0 dispositivos / 1 dispositivo / 2 dispositivos / 1.000.000 de dispositivos |
| ru | one, few, many, other | 1, 21 устройство; 2, 22 устройства; 0, 5, 11 устройств |
| lt | one, few, other (many optional) | 1, 21 įrenginys; 2, 9, 22 įrenginiai; 0, 10, 11, 20 įrenginių |
| zh-Hans | no variable: a plain `%lld` format; a sentence that never shows its count (only "it"/"them" agrees) keeps its variable with a single `other` form | 发现 %lld 个设备。 |

Russian and Lithuanian choose the form from the last digits (21 takes the singular, 11–19 the plural genitive), so never special-case only 1. CLDR puts Portuguese 0 in `one`, but Brazilian usage takes the plural (*0 dispositivos*), so every Portuguese plural variable also defines `zero`, worded like `other`; Foundation uses a zero form for exactly 0 and for no other count (verified 2026-09-25). French keeps 0 in the singular, which is correct French. Russian `other` and Lithuanian `many` apply only to fractions, but Russian still needs `other` because Foundation requires it as the fallback; use the genitive singular there.

How to write a plural entry:

- Declare one variable per count in `NSStringLocalizedFormatKey`, as `%#@name@`, with `NSStringFormatSpecTypeKey` = `NSStringPluralRuleType` and `NSStringFormatValueTypeKey` = `lld`. The app passes a 64-bit integer, which only `lld` reads correctly.
- A form shows only its own count, as `%lld`. Either every form of a variable shows the number or none does: a variable that only makes a verb agree (`1 of 2 banks matches`) reuses the count's position, `%1$lld … %1$#@verb@`, and its forms are just the verb.
- Put agreeing words inside the form when they change with the count: Russian and Lithuanian participles (`Найден 1 том` / `Найдено 5 томов`, `Rastas 1 įrenginys` / `Rasta 10 įrenginių`), Spanish and Portuguese verbs (`Se descubrió 1` / `Se descubrieron 2`), and French and Italian past participles.
- A count that follows a preposition takes that case in every form: Russian `из 21 банка` / `из 5 банков`, Lithuanian `iš 21 banko` / `iš 2 bankų`.
- Prefer natural sentences to the `Label: N` style, which remains only for count lists such as `PASS %d, WARN %d, FAIL %d`.
- `tests/test_localization_plurals.py` checks the structure and forms, and the Swift `PluralLocalizationTests` render every plural key in every language at each boundary count. Both take the plural categories from CLDR through the `babel` test dependency (`tests/fixtures/plural_categories.py` writes the Swift copy), so no rule is written by hand. If a babel update changes a category, rerun `python -m tests.fixtures.plural_categories --write`; a Swift failure then means Foundation and CLDR disagree, which is worth investigating rather than silencing.

## Wording decisions

- The approved English keeps **Activate** as the action name, including the NetBSD 4 notices that say "may need Activate". Translate these with the localized Activate label; do not turn them into automatic startup.
- Keep the approved English claims "Enable insecure SMB1", "Default setting", both AFP help descriptions, and "Much slower". Do not soften, expand or reconcile them while translating.
- ATA error messages say **non-negative number of seconds**, not "whole number" or "integer"; the wording does not change how the number is parsed.
- Maintenance still supports "No Reboot", and the NetBSD 4 deploy confirmations still describe reboot-then-activate. Keep those messages.
- **Reconnect saved device** links the discovered appliance to an existing saved profile after its identity is verified. It keeps that profile's settings, configuration path and credentials account; avoid wording that suggests restoring firmware or creating another saved device. "Device identity" identifies the appliance; a model name, Bonjour name or IP address alone does not. Keep the Apple property `waMA` and `MAC` unchanged in technical text.
- Identity conflicts point to the toolbar action **Forget**, by its exact label. Forget deletes a saved profile and its local credentials and configuration from this Mac; it does not reset or uninstall the appliance. Conflict guidance asks the user to review duplicate saved devices before using it. **Open saved device** goes to an existing profile without changing its identity or credentials.

## Checking a change

- Check key coverage in both `Localizable.strings` and `Localizable.stringsdict`, duplicate keys, format arguments, escapes and literal tokens. A translation equal to the English is not an error by itself: `Bonjour`, `Time Machine` and naturally identical words are fine.
- Run the localization tests (`tests/test_error_catalog.py`, `tests/test_summaries.py`, `tests/test_localization_style.py`, `tests/test_localization_plurals.py` and the Swift localization tests), and check how changed long labels render. `tests/test_localization_style.py` checks the mechanical rules here: each locale's quotation marks, apostrophes and ellipses, French no-break spaces, Chinese full-width punctuation, Russian е, Spanish *este Mac*, the form of address (pronouns and common commands), Forget against Uninstall, English quoting the labels it tells the user to use, and English errors and recovery naming operations instead of CLI words. They catch slips, not meaning: a new or reworded translation still needs a reader of that language.
- A copy change needs no device deployment or NetBSD rebuild.
