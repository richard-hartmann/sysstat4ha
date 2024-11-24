# Install

* If you havn't, get pipx and poetry.
  ```
  sudo apt install pipx
  pipx install poetry
  ```
* Clone git repo.
  ```
  cd <some path where you want to keep the code>
  git clone https://github.com/richard-hartmann/sysstat4ha.git
  cd sysstat4ha
  poetry install
  ```
* Customize the config
  ```
  cp s4h.toml my_s4h.toml
  ```
  and edit `my_s4h.toml`. (First three lines are necessary to edit. See also [Home Assistant Preparation](#home-assistant-preparation))

* Expose this computer to your Home Assistant using MQTT Discovery. To do so,
  enter poetry shell which provides the `s4h` command.
  ```
  poetry shell
  s4h exposer -c ./my_s4h.toml
  ```
  You should now see the new sensors in HA.

* Install systemd service to continuously publish sensor data.
  Generate install script and service file.
  ```
  s4h prepare_install -c ./my_s4h.toml
  ```
  Install service with
  ```
  sudo installer/install.sh
  ```
  and you should see live data in HA.

* Optionally, use the YAML `installer/card.yaml` as template to configure a nice card.
  Note that is using bar-card from HACS.

# Debug Problems

If the sensor data is not finding its way to HA, you can run the publishing with debug
messages.

* First stop the systemd service.
  ```
  sudo systemctl stop s4h.service
  ```
  
* Assuming you are still in the poetry shell, the command will publish and print debug messages.
  ```
  s4h publish -c my_s4h.toml -l debug
  ```

# Home Assistant Preparation

* install MQTT add on (Mosquitto broker)
* set custom user and pass word
  - go to Settings -> Add-ons -> 'Mosquitto broker' and hit the tab configuration
  - add login in YAML style, e.g.,
    ```
    - username: <some_username>
      password: <some_pw>
    ```