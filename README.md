# Home Assistant Preparation

* install MQTT add on (Mosquitto broker)
* set custom user and pass word
  - go to Settings -> Add-ons -> 'Mosquitto broker' and hit the tab configuration
  - add login in YAML style, e.g.,
    ```
    - username: <some_username>
      password: <some_pw>
    ```