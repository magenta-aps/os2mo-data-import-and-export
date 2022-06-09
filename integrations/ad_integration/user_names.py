import csv
import io
import logging
import re
import string
from functools import partial
from operator import itemgetter
from typing import List
from typing import Tuple

from more_itertools import flatten
from ra_utils.load_settings import load_setting
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import column
from sqlalchemy.sql import table
from unidecode import unidecode

from .ad_exceptions import ImproperlyConfigured
from .ad_reader import ADParameterReader
from .username_rules import method_2

logger = logging.getLogger(__name__)

NameType = List[str]


class UserNameGen:
    _setting_prefix = "integrations.ad_writer.user_names"

    @classmethod
    def get_implementation(cls) -> "UserNameGen":
        """Returns an implementation of `UserNameGen`.

        If you want to load occupied usernames from AD or elsewhere, you can
        call `load_occupied_names` on the instance returned by this method.

        By default, no occupied names are loaded on the instance returned by
        this method. This can be used in the cases where we want to spare the
        overhead of retrieving all AD usernames, for instance.
        """
        implementation_class = UserNameGenMethod2  # this is the default

        get_class_name = load_setting(f"{cls._setting_prefix}.class")

        try:
            name = get_class_name()
            implementation_class = cls._lookup_class_by_name(name)
        except (ValueError, FileNotFoundError):
            # ValueError: "not in settings file and no default"
            # FileNotFoundError: could not find "settings.json"
            logger.warning(
                "could not read settings, defaulting to %r",
                implementation_class,
            )
        except NameError:
            logger.warning(
                "could not find class %r, defaulting to %r",
                name,
                implementation_class,
            )

        instance = implementation_class()
        return instance

    @classmethod
    def _lookup_class_by_name(cls, name):
        if name and name in globals():
            return globals()[name]
        raise NameError(f"could not find class {name}")

    def __init__(self):
        self.occupied_names = set()
        self._loaded_occupied_name_sets = []

    def add_occupied_names(self, occupied_names: set) -> None:
        self.occupied_names.update(set(occupied_names))
        self._loaded_occupied_name_sets.append(occupied_names)

    def create_username(self, name: NameType, dry_run=False) -> str:
        raise NotImplementedError("must be implemented by subclass")

    def load_occupied_names(self):
        # Always load AD usernames when this method is called
        self.add_occupied_names(UserNameSetInAD())

        # Load any extra username sets specified in settings
        setting_name = f"{self._setting_prefix}.extra_occupied_name_classes"
        get_usernameset_class_names = load_setting(setting_name, default=[])
        try:
            usernameset_class_names = get_usernameset_class_names()
        except FileNotFoundError:  # could not find "settings.json"
            logger.info("could not read settings, not adding extra username sets")
        else:
            for name in usernameset_class_names:
                try:
                    cls = self._lookup_class_by_name(name)
                except NameError:
                    raise ImproperlyConfigured(
                        f"{setting_name!r} refers to unknown class {name!r}"
                    )
                else:
                    username_set = cls()
                    self.add_occupied_names(username_set)
                    logger.debug("added %r to set of occupied names", username_set)

    def is_username_occupied(self, username):
        return username.lower() in set(map(str.lower, self.occupied_names))


class UserNameGenMethod2(UserNameGen):
    """
    An implementation of metode 2 in the AD MOX specification document
    (Bilag: Tildeling af brugernavne).
    """

    def __init__(self):
        super().__init__()
        self.combinations = [
            method_2.FIRST,
            method_2.SECOND,
            method_2.THIRD,
            method_2.FOURTH,
            method_2.FIFTH,
            method_2.SIXTH,
        ]

    def _name_fixer(self, name: NameType) -> NameType:
        for i in range(0, len(name)):
            # Replace according to replacement list
            for char, replacement in method_2.CHAR_REPLACEMENT.items():
                name[i] = name[i].replace(char, replacement)

            # Remove all remaining characters outside a-z
            for char in name[i].lower():
                if ord(char) not in range(ord("a"), ord("z") + 1):
                    name[i] = name[i].replace(char, "")

        return name

    def _readable_combi(self, combi):
        readable_combi = []
        max_position = -1
        for i in range(0, len(combi)):
            # First name
            if combi[i] == "F":
                position = 0
            # First middle name
            if combi[i] == "1":
                position = 1
            # Second middle name
            if combi[i] == "2":
                position = 2
            # Third middle name
            if combi[i] == "3":
                position = 3
            # Last name (independent of middle names)
            if combi[i] == "L":
                position = -1
            if combi[i] == "X":
                position = None
            if position is not None and position > max_position:
                max_position = position
            readable_combi.append(position)
        return (readable_combi, max_position)

    def _create_from_combi(self, name, combi):
        """
        Create a username from a name and a combination.
        """
        (code, max_position) = self._readable_combi(combi)

        # Do not use codes that uses more names than the actual person has
        if max_position > len(name) - 2:
            return None

        # First letter is always first letter of first position
        if code[0] is not None:
            relevant_name = code[0]
            username = name[relevant_name][0].lower()
        else:
            username = "X"

        current_char = 0
        for i in range(1, len(code)):
            if code[i] == code[i - 1]:
                current_char += 1
            else:
                current_char = 0

            if code[i] is not None:
                relevant_name = code[i]
                if current_char >= len(name[relevant_name]):
                    username = None
                    break
                username += name[relevant_name][current_char].lower()
            else:
                username += "X"
        return username

    def create_username(self, name: NameType, dry_run: bool = False) -> str:
        """
        Create a new username in accodance with the rules specified in this file.
        The username will be the highest quality available and the value will be
        added to list of used names, so consequtive calles with the same name
        will keep returning new names until the algorithm runs out of options
        and a RuntimeException is raised.

        :param name: Name of the user given as a list with at least two elements.
        :param dry_run: If true the name will be returned, but will be added to
        the interal list of reserved names.
        :return: New username generated.
        """
        final_user_name = ""
        name = self._name_fixer(name)

        for permutation_counter in range(2, 10):
            for prioritation in range(0, len(self.combinations)):
                if final_user_name != "":
                    break
                i = 0
                for combi in self.combinations[prioritation]:
                    i = i + 1
                    username = self._create_from_combi(name, combi)
                    if not username:
                        continue
                    indexed_username = username.replace("X", str(permutation_counter))
                    if not self.is_username_occupied(indexed_username):
                        if not dry_run:
                            self.occupied_names.add(indexed_username)
                        final_user_name = indexed_username
                        return final_user_name

        # If we get to here, we completely failed to make a username
        raise RuntimeError("Failed to create user name")


class UserNameGenPermutation(UserNameGen):
    def __init__(self):
        super().__init__()
        self.length = 3
        self.consonants = "".join(set(string.ascii_lowercase) - set("aeiouy"))

    def create_username(self, name: NameType, dry_run: bool = False) -> str:
        suffix = 1
        while True:
            letters = self._extract_letters(name)
            new_username = "%s%d" % ("".join(letters), suffix)
            if not self.is_username_occupied(new_username):
                # An unused username was found, add it to the list of
                # occupied names and return.
                self.occupied_names.add(new_username)
                return new_username
            else:
                # We are still looking for an available username.
                # Bump the `suffix` variable.
                suffix += 1

    def _extract_letters(self, name: NameType):
        # Convert ["Firstname", "Last Name"] -> ["Firstname", "Last", "Name"]
        # and ["First-Name", "Last-Name"] -> ["First", "Name", "Last", "Name"]
        name = flatten(map(partial(re.split, r"[\-\s+]"), name))  # type: ignore

        # Convert all name parts to lowercase
        name = list(map(str.lower, name))

        # Check name parts
        first_ascii = set(name[0]) & set(string.ascii_lowercase)
        next_consonant = set("".join(name[1:])) & set(self.consonants)
        assert len(name) > 0, "name must have at least one part"
        assert first_ascii, "first name part must contain at least one ASCII character"
        assert next_consonant, "next name parts must contain at least one consonant"

        def only(allowed: str, part: str):
            return "".join(ch for ch in part if ch.lower() in allowed)

        result = []

        # Take first letter of first name part (regardless of whether it is a vowel or
        # a consonant.)
        result.append(only(string.ascii_lowercase, name[0])[0])

        # Continue at first letter of the second name part (first part if only one part)
        p = min(1, len(name) - 1)  # second name part (or first if only one part)
        offset = 0  # = first letter

        while len(result) < self.length:
            part = name[p]
            try:
                result.append(only(self.consonants, part)[offset])
            except IndexError:
                # Check if there are still more name parts to use
                if p < len(name) - 1:
                    # If yes, use next name part, starting at first letter
                    p += 1
                    offset = 0
                else:
                    # If no, go back to first name
                    p = 0
            else:
                offset += 1

        return result


class UserNameGenSvendborg(UserNameGen):
    def __init__(self):
        super().__init__()

    def create_username(self, name: NameType, dry_run: bool = False) -> str:
        parts = self._normalize_username(name)
        username = None

        if len(parts) < 2:
            raise ValueError("need at least two name parts to generate username")
        elif len(parts) == 2:
            username = self._get_username_for_case_a(parts)
        elif len(parts) > 2:
            username = self._get_username_for_case_b(parts)

        assert len(username) == 6  # type: ignore

        return username  # type: ignore

    def _normalize_username(self, name: NameType) -> NameType:
        # ["FirstName", "MiddleName LastName"] -> ["FirstName", "MiddleName", "LastName"]
        # ["FirstName", "MiddleName-LastName"] -> ["FirstName", "MiddleName", "LastName"]
        parts = flatten(map(partial(re.split, r"[\-\s+]"), name))
        # Uppercase each name part
        parts = map(str.upper, parts)
        # "Unaccent" any accented letters in each name part
        parts = map(unidecode, parts)
        return list(parts)

    def _get_username_for_case_a(self, parts: NameType) -> str:
        bits = (
            (3, 3),
            (2, 4),
            (4, 2),
            (5, 1),
            (1, 5),
        )

        for p1, p2 in bits:
            # Pad name parts with one or more 'X' characters if name part is too short
            # for the given values of `p1` and `p2`.
            if (len(parts[0]) < p1) or (len(parts[1]) < p2):
                parts = [parts[0].ljust(p1, "X"), parts[1].ljust(p2, "X")]
            new_username = "".join([parts[0][:p1], parts[1][:p2]])
            if not self.is_username_occupied(new_username):
                self.occupied_names.add(new_username)
                return new_username

        raise ValueError("could not create unique username (two name parts)")

    def _get_username_for_case_b(self, parts: NameType) -> str:
        bits = (
            (3, 0, 3),
            (2, 2, 2),
            (2, 0, 4),
            (4, 0, 2),
            (3, 1, 2),
            (2, 1, 3),
        )

        for p1, p2, p3 in bits:
            # Pad name parts with one or more 'X' characters if name part is too short
            # for the given values of `p1`, `p2` and `p3`.
            if (len(parts[0]) < p1) or (len(parts[1]) < p2) or (len(parts[-1]) < p3):
                parts = [
                    parts[0].ljust(p1, "X"),
                    parts[1].ljust(p2, "X"),
                    parts[-1].ljust(p3, "X"),
                ]
            new_username = "".join([parts[0][:p1], parts[1][:p2], parts[-1][:p3]])
            if not self.is_username_occupied(new_username):
                self.occupied_names.add(new_username)
                return new_username

        raise ValueError("could not create unique username (three or more name parts)")


class UserNameSet:
    def __init__(self):
        self._usernames = set()

    def __contains__(self, username: str) -> bool:
        return username.lower() in set(map(str.lower, self._usernames))

    def __iter__(self):
        return iter(self._usernames)


class UserNameSetCSVFile(UserNameSet):
    _encoding = "utf-8-sig"
    _column_name = "Brugernavn"

    def __init__(self):
        get_path = load_setting("integrations.ad_writer.user_names.disallowed.csv_path")
        with io.open(get_path(), "r", encoding=self._encoding) as stream:
            reader = csv.DictReader(stream)
            self._usernames = set(row[self._column_name] for row in reader)


class UserNameSetCSVFileSubstring(UserNameSetCSVFile):
    def __contains__(self, username: str) -> bool:
        for taken in self._usernames:
            if taken.lower() in username.lower():
                return True
        return False


class UserNameSetInAD(UserNameSet):
    def __init__(self):
        reader = ADParameterReader()
        self._usernames = reader.get_all_samaccountname_values()


class UserNameSetInDatabase(UserNameSet):
    def __init__(self):
        connection_string, table_name, column_name = self._get_settings()
        session = self._get_session(connection_string)
        t = table(table_name, column(column_name))
        res = session.query(t).distinct()
        # Extract the usernames from returned list of tuples like [('Alice',), ('Bob',)]
        self._usernames = set(map(itemgetter(0), res))

    def _get_settings(self) -> Tuple[str, str, str]:
        get_table_name = load_setting(
            "integrations.ad_writer.user_names.disallowed.sql_table_name"
        )
        get_column_name = load_setting(
            "integrations.ad_writer.user_names.disallowed.sql_column_name"
        )
        get_connection_string = load_setting(
            "integrations.ad_writer.user_names.disallowed.sql_connection_string"
        )
        return get_connection_string(), get_table_name(), get_column_name()

    def _get_session(self, connection_string):
        engine = create_engine(connection_string)
        return sessionmaker(bind=engine, autoflush=False)()
