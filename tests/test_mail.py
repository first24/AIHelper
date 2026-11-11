import unittest

from assistant.mail import address


class AddressTests(unittest.TestCase):
    def test_valid_qq_and_regular_addresses_are_preserved(self):
        for value in (
            'person@example.com',
            '123456@qq.com',
            'person@vip.qq.com',
            'user.name+tag@sub.example.co.uk',
            'person@my-domain.example.com',
        ):
            with self.subTest(value=value):
                self.assertEqual(address(value), value)

    def test_invalid_domain_labels_are_rejected(self):
        for value in (
            'person@-example.com',
            'person@example-.com',
            'person@.example.com',
            'person@example.com.',
            'person@' + 'a' * 64 + '.com',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                address(value)

    def test_consecutive_dots_in_domain_are_rejected(self):
        with self.assertRaises(ValueError):
            address('person@example..com')


if __name__ == '__main__':
    unittest.main()
