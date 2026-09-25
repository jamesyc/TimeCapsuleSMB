# macOS localization glossary

Reviewed 2026-09-22. Applies to `en`, `de`, `nl`, `fr`, `es`, `it`, `pt`, and `ru`, plus Simplified Chinese (`zh-Hans`). Lithuanian is not covered by this glossary.

This is an editorial guide, not a global search-and-replace dictionary. Keep the meaning of each operation, the distinction between an action and a status, and the distinction between an application and the service/storage it manages.

## Authority and scope

1. Explicit user-approved wording takes precedence over style preferences. The six settings descriptions approved on 2026-09-22 remain the English source; report factual concerns separately rather than silently changing their claims.
2. For names of macOS applications, settings, and buttons, use the name shown in Apple's documentation for that language. Sources below establish terminology, not the correctness of this app's behavior.
3. For our own UI, use natural technical language. The preferred translations below are project editorial choices unless marked as Apple application names. They are not all quotations from Apple.
4. Preserve literal program names, filenames, paths, flags, environment variables, API fields, device identifiers, and user-supplied names. A translated label must not imply that a file or executable has been renamed.
5. Portuguese currently mixes regions. This glossary **uses Brazilian Portuguese** for the existing `pt` catalog because much of its existing UI already uses Brazilian forms. Do not change locale routing or claim that `pt` is `pt-BR`; regional scope remains explicit; this does not introduce locale routing changes.

## Protected names and tokens

Keep `TimeCapsuleSMB`, `Samba`, `Netatalk`, `Bonjour`, `NetBSD`, `macOS`, `AirPort`, `AirPort Extreme`, `AirPort Express`, `SMB`, `SMB1`, `SMB2/3`, `AFP`, `SSH`, `ACP`, `NBNS`, `NetBIOS`, `DNS`, `DHCP`, `TCP`, `UDP`, `IPv4`, `IPv6`, `HFS`, `APFS`, `ACL`, `ATA`, `API`, `URL`, `UUID`, and `SHA-256` recognizable and correctly capitalized.

Keep literal `rsync`, `fsck`, `smbd`, `wcifsnd`, `mDNSResponder`, `vfs_aio_fork`, `ShareRoot`, `rc.local`, `xattr.tdb`, `.env`, and command/parameter spelling unchanged. Translate the surrounding verbs: Dutch `fsck uitvoeren`, French `Exécuter fsck`, Chinese `运行 fsck`.

Preserve `%@`, `%d`, `%lld`, positional specifiers, `%%`, URLs, backticks, and intentional `\n` escapes. If a translation needs argument reordering, use supported positional specifiers and verify it with a rendered example; never reorder bare placeholders casually. Do not translate literal backend status codes in raw logs. Translate the labels in the app's own status summary.

## Apple names: localized where users recognize a local name

- Apple: retain `Apple` in European-language product/company references. Chinese prose may use **苹果**, as requested by the maintainer; retain exact compound product names, source titles, URLs, and identifiers. Never translate the company to Russian `Яблоко`.
- Time Capsule: retain the hardware name **Time Capsule** in device selection and identification. Apple's Chinese docs also call it **时间返回舱**; explain as `Time Capsule（时间返回舱）` on a first explanatory mention if useful. Do not rename the project `TimeCapsuleSMB` or a discovered device instance. This is a deliberate recognition choice, not a claim that Apple never localizes it. [Chinese AirPort guide](https://support.apple.com/zh-cn/guide/aputility/welcome/mac)
- Time Machine: **时间机器** in Chinese user-facing prose; keep **Time Machine** in the other reviewed languages and in identifiers. [Chinese backup guide](https://support.apple.com/zh-cn/104984)
- Finder: **访达** in Chinese; **Finder** elsewhere. [Chinese guide using Finder and Disk Utility names](https://support.apple.com/zh-cn/102184)

| Language | Keychain store | Keychain Access application | Disk Utility application |
|---|---|---|---|
| English | keychain | Keychain Access | Disk Utility |
| German | Schlüsselbund | [Schlüsselbundverwaltung](https://support.apple.com/de-de/guide/keychain-access/welcome/mac) | [Festplattendienstprogramm](https://support.apple.com/de-de/guide/disk-utility/welcome/mac) |
| Dutch | sleutelhanger | [Sleutelhangertoegang](https://support.apple.com/nl-nl/guide/keychain-access/welcome/mac) | [Schijfhulpprogramma](https://support.apple.com/nl-nl/guide/disk-utility/welcome/mac) |
| French | trousseau | [Trousseaux d’accès](https://support.apple.com/fr-fr/guide/keychain-access/welcome/mac) | [Utilitaire de disque](https://support.apple.com/fr-fr/guide/disk-utility/welcome/mac) |
| Spanish | llavero | [Acceso a Llaveros](https://support.apple.com/es-es/guide/keychain-access/welcome/mac) | [Utilidad de Discos](https://support.apple.com/es-es/guide/disk-utility/welcome/mac) |
| Italian | portachiavi | [Accesso Portachiavi](https://support.apple.com/it-it/guide/keychain-access/welcome/mac) | [Utility Disco](https://support.apple.com/it-it/guide/disk-utility/welcome/mac) |
| Portuguese (Brazil) | chaves | [Acesso às Chaves](https://support.apple.com/pt-br/guide/keychain-access/welcome/mac) | [Utilitário de Disco](https://support.apple.com/pt-br/guide/disk-utility/welcome/mac) |
| Russian | связка ключей | [Связка ключей](https://support.apple.com/ru-ru/guide/keychain-access/welcome/mac) | [Дисковая утилита](https://support.apple.com/ru-ru/guide/disk-utility/welcome/mac) |
| Simplified Chinese | 钥匙串 | [钥匙串访问](https://support.apple.com/zh-cn/guide/keychain-access/welcome/mac) | [磁盘工具](https://support.apple.com/zh-cn/guide/disk-utility/dskutl1027/mac) |

“Cannot read the password from Keychain” refers to the **store**, not to opening the Keychain Access app. Do not turn every storage error into an application error. The newer Passwords app is also not a replacement term for the keychain API.

Chinese application names additionally include [终端](https://support.apple.com/zh-cn/guide/terminal/welcome/mac), [系统设置](https://support.apple.com/zh-cn/guide/mac-help/mchldfdf4f86/mac), and [AirPort 实用工具](https://support.apple.com/zh-cn/guide/aputility/welcome/mac). Dutch uses [AirPort-configuratieprogramma](https://support.apple.com/nl-nl/guide/aputility/welcome/mac). Apple documents German **AirPort-Dienstprogramm** and French **Utilitaire AirPort** in its [German](https://support.apple.com/de-de/112560) and [French](https://support.apple.com/fr-fr/112560) application inventories; these older inventories are terminology evidence, not current OS compatibility guidance.

## Preferred UI vocabulary

Inflect these terms naturally; entries below are base forms, not sentence fragments to splice mechanically.

| Concept | de | nl | fr | es |
|---|---|---|---|---|
| Device profile | Geräteprofil | apparaatprofiel | profil de l’appareil | perfil del dispositivo |
| Save profile | Profil speichern | Profiel opslaan | Enregistrer le profil | Guardar perfil |
| Diagnostics | Diagnose | Diagnostiek | Diagnostic | Diagnóstico |
| Helper program | Hilfsprogramm | hulpprogramma | programme auxiliaire | programa auxiliar |
| Discovery | Erkennung | detectie | découverte | descubrimiento |
| Running operation | Wird ausgeführt | Bezig | En cours d'exécution | En ejecución |
| Passed check | Bestanden | Geslaagd | Réussi | Aprobado |
| Warning | Warnung | Waarschuwing | Avertissement | Advertencia |
| Failed check | Fehlgeschlagen | Mislukt | Échec | Error |
| Network share | Freigabe | share | partage | recurso compartido |
| Firmware | Firmware | firmware | firmware | firmware |
| Metadata | Metadaten | metadata | métadonnées | metadatos |
| Extended attributes | erweiterte Attribute | uitgebreide attributen | attributs étendus | atributos extendidos |
| Authentication | Authentifizierung | authenticatie | authentification | autenticación |
| Encryption | Verschlüsselung | versleuteling | chiffrement | cifrado |
| Signing | Signierung | ondertekening | signature | firma |
| Disk mount | einbinden | koppelen | monter | montar |
| Snapshot | Momentaufnahme | momentopname | instantané | instantánea |
| Permissions | Zugriffsrechte | toegangsrechten | autorisations | permisos |
| Standby | Standby | stand-by | veille | modo de espera |
| Disk I/O | Festplatten-I/O | schijf-I/O | E/S disque | E/S de disco |

| Concept | it | pt-BR | ru | zh-Hans |
|---|---|---|---|---|
| Device profile | profilo del dispositivo | perfil do dispositivo | профиль устройства | 设备配置 |
| Save profile | Salva profilo | Salvar perfil | Сохранить профиль | 保存配置 |
| Diagnostics | Diagnostica | Diagnóstico | Диагностика | 诊断 |
| Helper program | programma ausiliario | programa auxiliar | вспомогательная программа | 辅助程序 |
| Discovery | rilevamento | descoberta | обнаружение | 发现 |
| Running operation | In esecuzione | Em execução | Выполняется | 运行中 |
| Passed check | Superato | Aprovado | Пройдено | 通过 |
| Warning | Avviso | Aviso | Предупреждение | 警告 |
| Failed check | Non riuscito | Falha | Ошибка | 失败 |
| Network share | condivisione | compartilhamento | общий ресурс | 共享 |
| Firmware | firmware | firmware | прошивка | 固件 |
| Metadata | metadati | metadados | метаданные | 元数据 |
| Extended attributes | attributi estesi | atributos estendidos | расширенные атрибуты | 扩展属性 |
| Authentication | autenticazione | autenticação | аутентификация | 身份验证 |
| Encryption | crittografia | criptografia | шифрование | 加密 |
| Signing | firma | assinatura | подпись | 签名 |
| Disk mount | montare | montar | монтировать | 挂载 |
| Snapshot | istantanea | snapshot | снимок | 快照 |
| Permissions | permessi | permissões | права доступа | 权限 |
| Standby | standby | modo de espera | режим ожидания | 待机 |
| Disk I/O | I/O del disco | E/S de disco | дисковый ввод-вывод | 磁盘 I/O |

Technical borrowings can be correct: Dutch `share`, Italian `log`, and Portuguese `snapshot` are acceptable in this app. Do not replace them with awkward literal inventions merely to remove English. Conversely, `Profile`, `Diagnostics`, `running`, and `bundled` are not protected names.

## Context-sensitive terms

| Source term | Decision |
|---|---|
| payload | Deployment files: say “installation files” / “Samba files” in the local language. JSON response: say “response data”. Firmware image segment: preserve the technical distinction, e.g. “firmware payload”; do not describe it as a complete firmware image. No global replacement. |
| runtime | User-facing service status: prefer “services” or “service status”. Technical execution environment: translate “runtime environment”. Do not turn a running process into an abstract “runtime” noun everywhere. |
| backend | Prefer “background operation” in user-facing progress; retain `backend` where describing the actual app/helper architecture or raw events. |
| boot hook | Describe as a startup hook / startup mechanism in the local language; retain `boot hook` parenthetically in specialist firmware UI if it helps recognition. This is not the bootloader itself. |
| firmware bank | A firmware storage bank/slot, not a financial bank, disk partition, or hardware memory bank unless the source says so. Preserve primary/inactive/active distinctions. |
| flash | Flash storage or the act of writing it; not a light flash. French “mémoire flash”, Russian “флеш-память”, Chinese “闪存”. |
| advertise | Announce a network service: German “ankündigen”, Dutch “aankondigen”, French “annoncer”, Spanish “anunciar”, Italian “annunciare”, Portuguese “anunciar”, Russian “объявлять”, Chinese “广播”. Avoid commercial advertising verbs. |
| idle | Inactive/waiting status. ATA idle timer and ATA standby timer are separate settings; do not merge them into a single sleep timer. |
| root | Filesystem root: local term. Unix `root` account: literal `root`. Root certificate: established certificate term. `ShareRoot`: literal identifier. |
| plan / dry run | A preview of actions, not an execution or activation of a “plan”. Preserve the distinction between planning, confirming, and performing writes. |
| repair / restore / reset | Repair damage; restore a previous/original state; reset editable values. Do not collapse all three into one verb. “Forget device” only removes the local saved profile; it does not uninstall Samba. |
| pass / fail | Test result, never walking past, passing an object, or physical running. Localize UI result labels, preserve machine-readable status codes. |

Chinese `挂载` is our general technical term; [Disk Utility calls its button 装载](https://support.apple.com/zh-cn/guide/disk-utility/dskutl1027/mac) and [uses 卸载](https://support.apple.com/zh-cn/guide/disk-utility/dskud709f49b/mac). Quote exact OS button text when giving OS instructions. Apple uses `宗卷` for a volume; `卷` is acceptable concise technical prose in our app, but neither means an entire physical disk. [Apple's SMB guidance](https://support.apple.com/zh-cn/102064) confirms `共享` and `元数据`; its [certificate guide](https://support.apple.com/zh-cn/guide/keychain-access/mchlp2697/mac) confirms `数字签名`. Use `SMB 签名` for the setting rather than adding a claim about certificate-based digital signatures.

## Style and review rules

- Translate complete sentences. Retaining a technical word does not justify broken local word order or missing grammatical agreement.
- Use concise action labels. Use an action verb for a button and a state/result for a status. Match references to other screen titles to their actual localized labels.
- Prefer sentence case except language-required noun capitalization and exact OS/product names. Do not mechanically propagate English title case.
- Use consistent voice within a locale. German buttons use infinitives; Dutch buttons normally place the verb last; Russian buttons use infinitives. Avoid mixing polite commands, infinitives, and noun phrases arbitrarily.
- Portuguese proposals use `arquivo`, `compartilhamento`, `configuração`, `criptografia`, `salvar`, and `planejar`; avoid mixing these with European `ficheiro`, `partilha`, `definição`, `encriptação`, `guardar`, and `planear` in the same catalog.
- Preserve meaning and severity in errors, especially “not”, “only”, “before”, “after”, “may”, and “must”. Do not strengthen “may help” into a guarantee.
- Do not erase technical detail merely to shorten a warning. Conversely, do not insert new behavioral claims during translation.
- Check key coverage, duplicate keys, format arguments, escapes, and literal tokens. Equality with English is not itself an error: names such as `Bonjour`, `Time Machine`, and naturally identical words are legitimate.
- Verify copy changes with localization tests plus focused render checks for changed long labels. Copy changes do not require a device deployment or a NetBSD rebuild.

## Additional verified OS application names

| Language | System Settings | AirPort Utility |
|---|---|---|
| German | [Systemeinstellungen](https://support.apple.com/de-de/guide/mac-help/mh15217/mac) | [AirPort-Dienstprogramm](https://support.apple.com/de-de/guide/aputility/welcome/mac) |
| Dutch | [Systeeminstellingen](https://support.apple.com/nl-nl/guide/mac-help/mh15217/mac) | [AirPort-configuratieprogramma](https://support.apple.com/nl-nl/guide/aputility/welcome/mac) |
| French | [Réglages Système](https://support.apple.com/fr-fr/guide/mac-help/mh15217/mac) | [Utilitaire AirPort](https://support.apple.com/fr-fr/guide/aputility/welcome/mac) |
| Spanish | [Ajustes del Sistema](https://support.apple.com/es-es/guide/mac-help/mh15217/mac) | [Utilidad AirPort](https://support.apple.com/es-es/guide/aputility/aprtc6ff2ed9/mac) |
| Italian | [Impostazioni di Sistema](https://support.apple.com/it-it/guide/mac-help/mh15217/mac) | [Utility AirPort](https://support.apple.com/it-it/guide/aputility/aprtc6ff2ed9/mac) |
| Portuguese (Brazil) | [Ajustes do Sistema](https://support.apple.com/pt-br/guide/mac-help/mh15217/mac) | [Utilitário AirPort](https://support.apple.com/pt-br/guide/aputility/-aprtc6ff2ed9/mac) |
| Russian | [Системные настройки](https://support.apple.com/ru-ru/guide/mac-help/mh15217/mac) | [Утилита AirPort](https://support.apple.com/ru-ru/guide/aputility/aprtc6ff2ed9/mac) |

These are names of external OS applications. They need not dictate the name of this app's own Settings screen. Spanish terminology here follows Apple's Spain documentation for named OS UI; the general `es` catalog should avoid unnecessary regional slang. If the product chooses Latin American Spanish instead, verify those OS names against that locale rather than assuming they are identical.

`Doctor` in prose describes this project's diagnostic checks, not a physician. Use the same localized concept as the Checkup screen. Keep the literal CLI command `doctor` intact. Similarly, “check” is a test, not a bank cheque; “plan” is an action plan, not a subscription; “directory” is a filesystem folder, not a telephone directory; “flush” means complete pending writes, not erase the disk; “settle” means allow startup to stabilize, not settle a bill.

## Maintainer wording decisions

The approved English source keeps **Activate** as the action name, including the NetBSD4 notices that say “may need Activate”. Translate these references using the corresponding localized action label, rather than changing their meaning to automatic startup.

Keep the approved English claims “Enable insecure SMB1”, “Default setting”, both existing AFP help descriptions, and “Much slower”. Do not independently soften, expand, or reconcile these claims during translation.

The path label is **Path for xattrs repair**. Keep the technical abbreviation `xattrs` in translations of that label. ATA error messages use **non-negative number of seconds**, not “whole number” or “integer”; the wording change does not alter the integer parsing behavior.

The four old custom NBNS/mDNS advertiser upload strings and six unused install-flow labels were removed. They are not retained for history. Maintenance still supports “No Reboot”, and current NetBSD4 deploy confirmations still describe reboot-then-activate. Do not remove those live messages.
