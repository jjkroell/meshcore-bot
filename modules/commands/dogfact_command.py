#!/usr/bin/env python3
"""
Dog Fact command for the MeshCore Bot
Provides random dog facts as a hidden easter egg command
"""

import random

from ..models import MeshMessage
from .base_command import BaseCommand


class DogfactCommand(BaseCommand):
    """Handles dog fact commands - hidden easter egg.

    Responds to various dog-related keywords with random facts about dogs.
    This is designed as a hidden feature and does not appear in standard help listings.
    """

    # Plugin metadata
    name = "dogfact"
    keywords = ['dogfact', 'dog', 'woof', 'bark', 'puppy']
    description = "Get a random dog fact (hidden command)"
    category = "hidden"  # Hidden category so it won't appear in help
    cooldown_seconds = 3  # 3 second cooldown per user

    def __init__(self, bot):
        """Initialize the dogfact command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.dogfact_enabled = self.get_config_value('Dogfact_Command', 'enabled', fallback=True, value_type='bool')

        # Collection of dog facts - fallback if translations not available
        self.dog_facts_fallback = [
            "A dog's sense of smell is 10,000 to 100,000 times more acute than a human's. 👃",
            "Dogs have three eyelids, including one to keep the eye moist and protected. 👁️",
            "A dog's nose print is unique, much like a human fingerprint. 🐾",
            "Dogs can hear sounds up to 47,000-65,000 Hz, far beyond the human limit of 20,000 Hz. 👂",
            "The Basenji is the only dog breed that can't bark - it yodels instead. 🎵",
            "Puppies are born deaf, blind, and toothless. 🍼",
            "A dog's normal body temperature is 101-102.5°F (38.3-39.2°C). 🌡️",
            "Dogs have about 1,700 taste buds; humans have around 9,000. 👅",
            "The Greyhound can reach speeds of 45 mph, making it the fastest dog breed. 🏃",
            "Dogs curl up in a ball to sleep to protect their organs and conserve body heat. 😴",
            "A dog's whiskers help it sense objects and navigate in the dark. 📏",
            "Dalmatian puppies are born completely white and develop their spots as they age. ⚪",
            "Dogs' only sweat glands are between their paw pads. 🐾",
            "The average dog is about as intelligent as a two-year-old child. 🧠",
            "Dogs can smell diseases such as some cancers, diabetes, and impending seizures. 🩺",
            "A dog's wet nose helps it absorb scent chemicals. 💧",
            "Dogs dream, and studies show they enter REM sleep just like humans do. 💤",
            "The tallest dog on record, a Great Dane named Zeus, stood 44 inches at the shoulder. 📐",
            "The smallest dog on record was a Chihuahua just 3.8 inches tall. 🐕",
            "Dogs tilt their heads to hear and locate sounds better and to see past their muzzles. 🤔",
            "Petting a dog can lower blood pressure and reduce stress in humans. 🧘",
            "Dogs have a special membrane in their eyes called the tapetum lucidum that helps them see in low light. 🌙",
            "A dog's hearing is so sensitive it can identify its owner's car engine from a block away. 🚗",
            "The Norwegian Lundehund has six toes on each foot and can bend its head backward to touch its spine. 🦴",
            "Dogs process the world largely through smell - they have up to 300 million scent receptors. 👃",
            "Three dogs survived the sinking of the Titanic. 🚢",
            "A dog's sense of time is linked to smell - scents fade over the day, helping them anticipate routines. ⏰",
            "The Saint Bernard and Newfoundland are among the heaviest breeds, often exceeding 150 pounds. ⚖️",
            "Dogs kick backward after going to the bathroom to spread their scent using glands in their paws. 🐾",
            "A wagging tail doesn't always mean a happy dog - it signals arousal, which can be positive or negative. 🚩",
            "Dogs have 18 muscles controlling their ears. 👂",
            "The oldest known dog breed is likely the Saluki, depicted in Egyptian art from around 2100 BC. 🏺",
            "Dogs can learn more than 100 words and gestures. 📚",
            "A Border Collie named Chaser learned the names of over 1,000 objects. 🎾",
            "Dogs have belly buttons, left from the umbilical cord, though they're hard to see under fur. 🔎",
            "Newfoundland dogs have water-resistant coats and webbed feet, making them excellent swimmers. 🏊",
            "A dog's mouth exerts 150-300 pounds of pressure per square inch on average. 🦷",
            "The phrase 'raining cats and dogs' may date to 17th-century England. 🌧️",
            "Dogs sniff with each nostril separately, helping them determine which direction a smell came from. 🧭",
            "A dog's shoulder blades are unattached to the skeleton, allowing greater flexibility for running. 🦴",
            "Bloodhounds can follow a scent trail more than 130 hours old across long distances. 🔦",
            "Dogs have been human companions for at least 15,000 years, and possibly over 30,000. 🌍",
            "The Chow Chow and the Shar-Pei are the only breeds with fully black tongues. 👅",
            "Small dog breeds tend to live longer than large breeds, sometimes by several years. 📅",
            "A dog's nose is typically cool and moist because of mucus and evaporation, not health status. 👃",
            "Dogs can get jealous when their owner shows affection to another animal. 💔",
            "The Beatles song 'A Day in the Life' contains a whistle only dogs can hear. 🎶",
            "A dog's paw pads contain a network of blood vessels that keep them from freezing on cold ground. ❄️",
            "Puppies sleep 18-20 hours a day during their fastest growth period. 🛌",
            "Dogs have a 'righting reflex' that helps them reorient when falling, though less refined than a cat's. 🤸",
            "The Portuguese Water Dog was bred to herd fish into nets and retrieve gear from the water. 🎣",
            "A dog's field of vision is about 250 degrees, compared to about 190 degrees for humans. 👀",
            "Dogs see best in blue and yellow; they can't distinguish red from green. 🌈",
            "The Australian Cattle Dog 'Bluey' lived to 29 years, one of the oldest dogs ever recorded. 🎂",
            "Dogs yawn contagiously in response to their owners, a sign of empathy and bonding. 🥱",
            "A group of puppies from one litter is called a litter; a group of dogs can be called a pack. 🐕‍🦺",
            "Dogs' ancestors, wolves, were likely drawn to early human camps by food scraps. 🔥",
            "A dog's heart beats 60-140 times per minute, depending on its size. ❤️",
            "The Labrador Retriever has been the most popular dog breed in the US for decades. 🏆",
            "Dogs can be trained to detect a change in a person's scent seconds before a medical emergency. 🚑",
            "Puppies have 28 baby teeth that are replaced by 42 adult teeth. 🦷",
            "The Alaskan Malamute was bred to haul heavy freight; the Siberian Husky was bred for speed. 🛷",
            "A dog can pinpoint the source of a sound in about six hundredths of a second. ⏱️",
            "The Chihuahua, the smallest recognized breed, originated in Mexico. 🇲🇽",
            "Dogs can be trained to detect dangerously low blood sugar in people with diabetes. 🩸",
            "A dog's hearing is roughly four times more sensitive than a human's. 👂",
            "Puppies open their eyes around 10 to 14 days after birth. 👀",
            "The Saint Bernard was bred by Alpine monks for mountain rescue work. ⛰️",
            "A dog's tail is a continuation of its spine, made of many small vertebrae. 🦴",
            "The Australian Shepherd was actually developed in the western United States. 🇺🇸",
            "The Pug was bred as a lap dog for the emperors of China. 👑",
            "A dog's brain devotes about 40 times more area to smell than a human brain does. 🧠",
            "The Bloodhound's long ears and loose skin help sweep scent toward its nose. 👃",
            "The Dachshund's name means badger dog in German; it was bred to dig into burrows. 🦡",
            "The Rhodesian Ridgeback has a ridge of backward-growing hair along its spine. 〰️",
            "A dog's mouth is not cleaner than a human's; it simply carries different bacteria. 🦠",
            "Greyhound-type dogs appear in Egyptian art more than 4,000 years old. 🏺",
            "The Komondor's corded coat helped shield it from wolf bites while guarding flocks. 🐑",
            "Puppies are born without kneecaps, which form at around six months of age. 🦵",
            "In a study, a Border Collie named Rico recognized more than 200 spoken words. 📚",
            "The Poodle's fancy show trim began as a practical cut for retrieving in cold water. ✂️",
            "The Xoloitzcuintli, or Mexican Hairless Dog, was considered sacred by the Aztecs. 🇲🇽",
            "Dogs dream more often as puppies and as seniors than in middle age. 💤",
            "The Great Dane was developed in Germany, where it is called the Deutsche Dogge. 🇩🇪",
            "Small dogs' hearts beat faster than large dogs' hearts. ❤️",
            "The word canine comes from the Latin canis, meaning dog. 📜",
            "The Afghan Hound is one of the most ancient breeds, genetically close to early dogs. 🐺",
            "The Labrador Retriever originated on the island of Newfoundland, not Labrador. 🇨🇦",
            "A dog's nose works in stereo, helping it sense which direction a smell comes from. 🎧",
            "The Cavalier King Charles Spaniel is named for King Charles II, who doted on the breed. 👑",
            "Dogs have a scent-detecting Jacobson's organ in the roof of the mouth. 👃",
            "The Pekingese was bred to resemble a lion and was once owned only by Chinese royalty. 🦁",
            "Dogs can smell shifts in human body chemistry linked to fear and stress. 😨",
            "The Weimaraner is nicknamed the Grey Ghost for its silvery coat and pale eyes. 👻",
            "The Shih Tzu's name means little lion in Mandarin. 🦁",
            "Puppies learn bite inhibition from their mother and littermates before eight weeks old. 🐾",
            "The Bernese Mountain Dog was bred to pull carts of milk and cheese in Switzerland. 🧀",
            "The Irish Wolfhound is the tallest breed by average height, over seven feet tall on its hind legs. 📐",
            "Dogs process word meaning with the left brain and tone with the right, much like people. 🧠",
            "The Akita was bred in Japan to hunt bears and symbolizes health and long life. 🇯🇵",
            "The Boxer is named for its habit of boxing with its front paws during play. 🥊",
            "The Samoyed's upturned smile helps keep it from drooling and forming icicles. ❄️",
            "Floppy ears may be a side effect of tameness, part of so-called domestication syndrome. 🧬",
            "Dogs can follow a human's pointing gesture to find hidden food, a skill even chimps find hard. 👉",
            "The Dalmatian historically ran alongside horse-drawn carriages as a guard dog. 🐎"
        ]

    def get_dog_facts(self) -> list[str]:
        """Get dog facts from translations or fallback to hardcoded list.

        Returns:
            List[str]: A list of dog fact strings.
        """
        facts = self.translate_get_value('commands.dogfact.facts')
        if facts and isinstance(facts, list) and len(facts) > 0:
            return facts
        return self.dog_facts_fallback

    def get_help_text(self) -> str:
        """Get help text for the dogfact command.

        Returns:
            str: Empty string (to keep the command hidden).
        """
        # Return empty string so it doesn't appear in help
        return ""

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.dogfact_enabled:
            return False
        return super().can_execute(message)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the dog fact command.

        Selects a random dog fact and sends it to the user.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Get dog facts from translations or fallback
            dog_facts = self.get_dog_facts()

            # Get a random dog fact
            dog_fact = random.choice(dog_facts)

            # Send the dog fact
            await self.send_response(message, dog_fact)
            return True

        except Exception as e:
            self.logger.error(f"Error in dog fact command: {e}")
            await self.send_response(message, self.translate('commands.dogfact.error'))
            return True
