# Triangle

Scanner de concerts entre Lyon, Saint-Étienne et Clermont-Ferrand. Il tourne seul deux fois par jour sur GitHub, gratuitement, même quand votre ordinateur est éteint.

Il croise Ticketmaster, OpenAgenda et l'agenda ouvert de Clermont-Ferrand, puis vous prévient sur Telegram des nouveaux concerts, des dates de mise en vente, des rappels avant ouverture, des annulations et des changements de date. La page web affiche tout et permet de changer vos artistes et vos styles.

Comptez environ 20 minutes pour l'installation. Les clés ne vont jamais dans les fichiers : elles sont gardées dans les secrets GitHub.

## 1. Clé Ticketmaster

1. Créez un compte sur https://developer.ticketmaster.com.
2. Ouvrez votre application dans votre compte, puis copiez la **Consumer Key**.

## 2. Clé OpenAgenda (facultative, conseillée)

1. Créez un compte sur https://openagenda.com/signup.
2. Ouvrez https://openagenda.com/settings/apiKey et copiez la clé publique, qui commence par `oa_pk_`.

Sans cette clé, le scanner fonctionne avec Ticketmaster et l'agenda de Clermont-Ferrand.

## 3. Bot Telegram

1. Dans Telegram, écrivez à **@BotFather**, envoyez `/newbot` et suivez les instructions.
2. Copiez le jeton qu'il vous donne.
3. Ouvrez votre nouveau bot et envoyez-lui un message. Sans cela, il ne pourra pas vous écrire.
4. Écrivez à **@userinfobot** et copiez votre identifiant, une suite de chiffres.

## 4. Dépôt GitHub

1. Créez un compte sur https://github.com si besoin.
2. Créez un dépôt **public** nommé `concerts`. La page gratuite exige un dépôt public : vos artistes et vos concerts seront donc lisibles par tous, mais pas vos clés.
3. Décompressez le zip, puis utilisez **Add file > Upload files** et déposez tout le contenu du dossier.
4. Vérifiez que le dossier `.github` apparaît dans le dépôt. Sur Mac, il est masqué : appuyez sur Cmd + Maj + point dans le Finder pour l'afficher.

## 5. Secrets

Dans le dépôt, ouvrez **Settings > Secrets and variables > Actions > New repository secret**, et créez ces secrets :

| Nom | Valeur |
| --- | --- |
| `TICKETMASTER_API_KEY` | la Consumer Key |
| `OPENAGENDA_API_KEY` | la clé OpenAgenda, si vous en avez une |
| `TELEGRAM_BOT_TOKEN` | le jeton de BotFather |
| `TELEGRAM_CHAT_ID` | votre identifiant Telegram |

## 6. Page web

Dans **Settings > Pages**, choisissez **Deploy from a branch**, puis la branche `main` et le dossier `/docs`. Enregistrez.

La page sera disponible après une ou deux minutes à l'adresse `https://VOTRE-PSEUDO.github.io/concerts/`.

## 7. Jeton pour enregistrer les réglages

1. Ouvrez https://github.com/settings/personal-access-tokens/new.
2. Choisissez une expiration d'un an et limitez l'accès au dépôt `concerts`.
3. Dans **Permissions**, ajoutez **Contents** et **Actions** en **Read and write**.
4. Copiez le jeton et collez-le dans l'onglet **Réglages** de la page.

Le jeton reste dans le navigateur de chaque appareil. S'il expire, les scans continuent : seul l'enregistrement des réglages s'arrête jusqu'à ce que vous en créiez un nouveau.

## 8. Premier scan

Dans **Réglages**, choisissez vos artistes et vos styles, puis cliquez sur **Enregistrer et lancer le scan**. Vous recevez un message Telegram de confirmation après quelques minutes.

## Alertes à activer à côté

Le scanner ne lit pas Fnac Spectacles ni See Tickets. Pour réduire ce manque, activez les alertes **Salle** d'Infoconcert pour vos salles principales et suivez vos artistes dans l'application Songkick.

## Limites à connaître

- Seul Ticketmaster fournit les dates de mise en vente. OpenAgenda et l'agenda de Clermont-Ferrand signalent les concerts, sans préventes ni rappels.
- OpenAgenda et Clermont-Ferrand ne classent pas les concerts comme une billetterie. Le scanner les repère par des mots comme « concert » ou « showcase » : des événements peuvent manquer ou être de trop.
- Si une même date apparaît sur deux sources, le scanner garde la fiche Ticketmaster. Une fusion peut échouer si les titres sont trop différents.
- Seuls les événements OpenAgenda sous licence ouverte sont repris, avec leur source sur la page.
- L'onglet **Réglages > Dernier scan > État des sources** indique ce que chaque source a renvoyé.
- Les pages de salles comblent en partie les manques, mais seulement pour vos artistes. Une adresse illisible est signalée dans **Réglages > Dernier scan** : remplacez-la par la page d'agenda de la salle.
- Les heures des scans sont indicatives, car GitHub peut les retarder.
- Après trois scans ratés de suite, Telegram vous prévient.

## Pour aller plus loin

- Modifier les styles proposés : `docs/styles.json`.
- Changer les heures de scan : `.github/workflows/scan.yml`.
- Lancer les tests : `python3 -m unittest discover -s scanner`.

## Mise à jour d'une installation existante

Déposez uniquement les fichiers du zip de mise à jour. Il ne contient ni `docs/config.json`, ni `docs/data`, ni `state`, pour conserver vos réglages et votre historique. Ajoutez ensuite le secret `OPENAGENDA_API_KEY`. Les concerts déjà annoncés par une nouvelle source sont enregistrés sans alerte lors de son premier passage.
